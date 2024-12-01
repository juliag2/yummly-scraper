from bs4 import BeautifulSoup
import undetected_chromedriver as uc
import json
import os
import time
import random
import re
import urllib.parse
import traceback

from selenium import webdriver
from selenium.webdriver.common.by import By

def fetch_sitemap(sitemap):
    """
    Fetch and parse a Yummly sitemap XML
    """
    try:
        file = open(sitemap, 'r')
        content = file.read()
        file.close()

        # Use lxml parser for more robust XML parsing
        soup = BeautifulSoup(content, 'lxml-xml')

        # Explicitly find all <loc> tags within <url> tags
        urls = [url.find('loc').text for url in soup.find_all('url')]

        print(f"Parsed {len(urls)} URLs from sitemap")
        return urls
    except Exception as e:
        print(f"Error reading sitemap {sitemap}: {e}")
        return []


def extract_initial_state(url, driver):
    """
    Extract the window.__INITIAL_STATE__ content from a Yummly recipe page
    """
    try:
        # Fetch the page
        driver.get(url)
        wait_for_cloudflare(driver)

        title = driver.find_element(By.TAG_NAME, "title")
        if 'error' in title.get_attribute('innerHTML').lower():
            print("Error page detected")
            return None

        scripts = driver.find_elements(By.TAG_NAME, "script")

        initial_state = None

        for script in scripts:
            if "window.__INITIAL_STATE__" in script.get_attribute('innerHTML'):
                initial_state = script.get_attribute('innerHTML')
                break

        if not initial_state:
            print("Could not find window.__INITIAL_STATE__ in the page")
            return None
        # Use regex to find the __INITIAL_STATE__ script
        initial_state_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*\"\s*(.+)\"', initial_state)

        if not initial_state_match:
            print("Could not extract __INITIAL_STATE__ from the page")
            return None

        initial_state_str = initial_state_match.group(1)

        # URL decode the initial state
        decoded_initial_state = urllib.parse.unquote(initial_state_str)

        # Parse the JSON
        try:
            initial_state = json.loads(decoded_initial_state)

            # Extract the recipe data
            recipe_data = initial_state.get('recipe')

            message = recipe_data.get('message')
            if recipe_data and not (message and message.startswith("recipe not found")):
                print("Successfully extracted recipe data")
                return initial_state
            else:
                print("No recipe data found in __INITIAL_STATE__")
                return None
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON: {e}")
            print("Problematic JSON string:", decoded_initial_state[:1000])  # Print first 1000 chars for debugging
            return None
    except Exception as e:
        print(f"Error extracting initial state from {url}: {e}")
        print(traceback.format_exc())
        return None

def strip_recipe_data(all_data, scraped_urls, failed_urls):
    """
        Extract relevant data from recipe JSON, discard unnecessary data.
        Also extracts related recipes, so they don't have to be scraped.
    """
    data = []
    more_from_source = []
    recipe_data = all_data.get('recipe')
    if recipe_data is None:
        return data
    yums = all_data.get('yums') or all_data.get('yumsObject')
    recipe_data['yums'] = yums
    if 'moreFromSource' in recipe_data:
        for related_recipe in recipe_data.get('moreFromSource'):
            info = related_recipe.get('recipeInfo')
            wrapped_data = strip_recipe_data(info, scraped_urls, failed_urls)
            more_from_source.append(related_recipe.get('id'))
            data.extend(wrapped_data)
        recipe_data.pop('moreFromSource')
        recipe_data.pop('moreFromSourceLoaded')
        recipe_data.pop('moreFromSourceLoading')

    if 'relatedRecipes' in recipe_data:
        for related_recipe in recipe_data.get('relatedRecipes'):
            info = related_recipe.get('recipeInfo')
            wrapped_data = strip_recipe_data(info, scraped_urls, failed_urls)
            more_from_source.append(related_recipe.get('id'))
            data.extend(wrapped_data)
        recipe_data.pop('relatedRecipes')
        recipe_data.pop('relatedRecipesLoaded')
        recipe_data.pop('relatedRecipesLoading')

    if 'spotlightCarousels' in recipe_data:
        for carousel in recipe_data.get('spotlightCarousels'):
            for related_recipe in carousel.get('cards').get('newList'):
                info = related_recipe.get('recipeInfo')
                wrapped_data = strip_recipe_data(info, scraped_urls, failed_urls)
                more_from_source.append(related_recipe.get('id'))
                data.extend(wrapped_data)
        recipe_data.pop('spotlightCarousels')
        recipe_data.pop('spotlightCarouselsLoaded')
        recipe_data.pop('spotlightCarouselsLoading')

    scraped_urls.add(recipe_data.get('share').get('url'))

    data.append(recipe_data)

    return data

def wait_for_cloudflare(driver):
    count = 0
    while True:
        title = driver.find_element(By.TAG_NAME, "title")

        while not title:
            time.sleep(0.2)
            title = driver.find_element(By.TAG_NAME, "title")

        if 'yummly' in title.get_attribute('innerHTML').lower():
            return
        else:
            print(title.get_attribute('innerHTML'))
        time.sleep(1)
        count += 1
        if count > 5:
            input("Please solve the Cloudflare challenge and press Enter to continue...")


def scrape_yummly_recipes(output_dir='yummly_recipes'):
    """
    Scrape recipes from Yummly sitemaps
    """

    # Create a undetected_chromedriver instance to avoid bot detection
    options = uc.ChromeOptions()
    options.headless = False

    driver = uc.Chrome(options=options)

    driver.get("https://yummly.com/")
    input("Press enter once you are past cloudflare...")

    # Create output directories
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, 'recipes'), exist_ok=True)

    sitemap_dir = os.path.join(output_dir, 'sitemaps')

    skipped = 0
    count = 0

    start_time = time.time()

    # Load progress
    if os.path.exists(os.path.join(output_dir, 'scraped_urls.txt')):
        with open(os.path.join(output_dir, 'scraped_urls.txt'), 'r') as f:
            scraped_urls = set(f.read().splitlines())
    else:
        scraped_urls = set()
    if os.path.exists(os.path.join(output_dir, 'failed_urls.txt')):
        with open(os.path.join(output_dir, 'failed_urls.txt'), 'r') as f:
            failed_urls = set(f.read().splitlines())
    else:
        failed_urls = set()

    for sitemap in os.listdir(sitemap_dir):
        print(f"Processing sitemap: {sitemap}")

        recipe_urls = fetch_sitemap(os.path.join(sitemap_dir, sitemap))
        print(f"Found {len(recipe_urls)} URLs in sitemap")

        for url in recipe_urls:
            # Avoid duplicates and implement rate limiting
            if url in scraped_urls or url in failed_urls:
                skipped += 1
                continue

            print(f'New URL: {url}')

            # Random delay to be nice to the server
            time.sleep(random.uniform(0.5, 2))

            # The __INITIAL_STATE__ contains all data about the page, including the recipes
            initial_state = extract_initial_state(url, driver)

            if initial_state:
                for recipe_data in strip_recipe_data(initial_state, scraped_urls, failed_urls):
                    count += 1
                    r_id = recipe_data.get('id')
                    # Save recipe data
                    recipe_filename = os.path.join(output_dir, 'recipes', f"{hash(r_id)}.json")
                    with open(recipe_filename, 'w', encoding='utf-8') as f:
                        json.dump(recipe_data, f, ensure_ascii=False, indent=2)

                    scraped_urls.add(url)
            else:
                count += 1
                failed_urls.add(url)

            # Save progress every 100 URLs
            if count > 100:
                count = 0
                print("Saving progress...")
                with open(os.path.join(output_dir, 'scraped_urls.txt'), 'w') as f:
                    f.write('\n'.join(scraped_urls))
                with open(os.path.join(output_dir, 'failed_urls.txt'), 'w') as f:
                    f.write('\n'.join(failed_urls))
                print(f"Successfully scraped {len(scraped_urls)} URLs, Failed {len(failed_urls)} URLs. Skipped {skipped} URLs. Took {time.time() - start_time} seconds.")

    # Final save of progress
    with open(os.path.join(output_dir, 'scraped_urls.txt'), 'w') as f:
        f.write('\n'.join(scraped_urls))
    with open(os.path.join(output_dir, 'failed_urls.txt'), 'w') as f:
        f.write('\n'.join(failed_urls))

if __name__ == "__main__":
    scrape_yummly_recipes()
