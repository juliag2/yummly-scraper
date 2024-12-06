from bs4 import BeautifulSoup
import undetected_chromedriver as uc
import json
import os
import time
import random
import re
import urllib.parse
import traceback
from curl_cffi import requests

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


def extract_initial_state(url, session):
    """
    Extract the window.__INITIAL_STATE__ content from a Yummly recipe page
    """
    try:
        resp = get(url, session)
        if resp is None:
            return None

        if 'error' in resp.title.string.lower():
            print("Error page detected")
            return None

        scripts = resp.find_all('script')

        initial_state = None

        for script in scripts:
            strings = list(script.stripped_strings)
            if len(strings) == 0:
                continue
            if "window.__INITIAL_STATE__" in strings[0]:
                initial_state = strings[0]
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

def get(url:str, session:requests.Session, retry_count:int = 0) -> BeautifulSoup | None:
    response = session.get(url)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        if retry_count < 3:
            time.sleep(pow(3, retry_count + 1))
            return get(url, session, retry_count + 1)
        else:
            print(f"Failed to get {url} after 3 retries")
            return None
    soup = BeautifulSoup(response.text, 'html.parser')
    if not "yummly" in soup.title.string.lower():
        opts = uc.ChromeOptions()
        opts.headless = False
        driver = uc.Chrome(options=opts)
        driver.get(url)
        input("Press enter once you are past cloudflare...")
        session = get_session_from_selenium(driver)
        driver.quit()
        response = session.get(url)
        if response.status_code != 200:
            return response
        soup = BeautifulSoup(response.text, 'html.parser')

        if not "yummly" in soup.title.string.lower():
            exit("Failed to bypass cloudflare")
    return soup

def get_session_from_selenium(driver):
    """
    Create a requests session with cookies and headers from selenium
    """
    session = requests.Session(impersonate = "chrome131")

    # Copy cookies from selenium to requests
    for cookie in driver.get_cookies():
        session.cookies.set(cookie['name'], cookie['value'])

    return session

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
    session = get_session_from_selenium(driver)
    driver.quit()

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
            time.sleep(random.uniform(0.2, 0.5))

            # The __INITIAL_STATE__ contains all data about the page, including the recipes
            initial_state = extract_initial_state(url, session)

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
