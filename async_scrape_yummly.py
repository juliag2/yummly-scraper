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
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Set, List, Dict, Any, Optional
from dataclasses import dataclass
import dataclasses
import aiofiles

@dataclass
class ScraperState:
    scraped_urls: Set[str]
    failed_urls: Set[str]
    output_dir: str
    session: requests.Session
    time: float
    scraped_count: int = 0
    failed_count: int = 0
    processed_count: int = 0
    skipped_count: int = 0
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)

async def save_progress(state: ScraperState):
    async with state.lock:
        """Save the current progress to files"""
        async with aiofiles.open(os.path.join(state.output_dir, 'scraped_urls.txt'), 'w') as f:
            await f.write('\n'.join(state.scraped_urls))
        async with aiofiles.open(os.path.join(state.output_dir, 'failed_urls.txt'), 'w') as f:
            await f.write('\n'.join(state.failed_urls))

async def save_recipe(output_dir: str, recipe_data: Dict[str, Any]):
    """Save a recipe to a JSON file"""
    r_id = recipe_data.get('id')
    recipe_filename = os.path.join(output_dir, 'recipes', f"{hash(r_id)}.json")
    async with aiofiles.open(recipe_filename, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(recipe_data, ensure_ascii=False, indent=2))

async def strip_recipe_data(all_data):
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
            wrapped_data = await strip_recipe_data(info)
            more_from_source.append(related_recipe.get('id'))
            data.extend(wrapped_data)
        recipe_data.pop('moreFromSource')
        recipe_data.pop('moreFromSourceLoaded')
        recipe_data.pop('moreFromSourceLoading')

    if 'relatedRecipes' in recipe_data:
        for related_recipe in recipe_data.get('relatedRecipes'):
            info = related_recipe.get('recipeInfo')
            wrapped_data = await strip_recipe_data(info)
            more_from_source.append(related_recipe.get('id'))
            data.extend(wrapped_data)
        recipe_data.pop('relatedRecipes')
        recipe_data.pop('relatedRecipesLoaded')
        recipe_data.pop('relatedRecipesLoading')

    if 'spotlightCarousels' in recipe_data:
        for carousel in recipe_data.get('spotlightCarousels'):
            for related_recipe in carousel.get('cards').get('newList'):
                info = related_recipe.get('recipeInfo')
                wrapped_data = await strip_recipe_data(info)
                more_from_source.append(related_recipe.get('id'))
                data.extend(wrapped_data)
        recipe_data.pop('spotlightCarousels')
        recipe_data.pop('spotlightCarouselsLoaded')
        recipe_data.pop('spotlightCarouselsLoading')


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


def extract_initial_state(url, session):
    """
    Extract the window.__INITIAL_STATE__ content from a Yummly recipe page
    """
    try:
        resp = get(url, session)
        if resp is None:
            return None

        if 'error' in resp.title.string.lower():
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


async def process_url(url: str, state: ScraperState, semaphore: asyncio.Semaphore) -> None:
    """Process a single URL with rate limiting"""
    async with state.lock:
        if url in state.scraped_urls or url in state.failed_urls:
            state.skipped_count += 1
            return

    async with semaphore:  # Limit concurrent requests
        try:
            async with state.lock:
                session = state.session
            initial_state = await asyncio.get_event_loop().run_in_executor(
                None, extract_initial_state, url, session
            )

            if initial_state:
                recipe_data_list = await strip_recipe_data(initial_state)
                state.scraped_count += len(recipe_data_list)
                for recipe_data in recipe_data_list:
                    async with state.lock:
                        state.scraped_urls.add(recipe_data.get('share').get('url'))
                        state.scraped_count += 1
                    await save_recipe(state.output_dir, recipe_data)
            else:
                async with state.lock:
                    state.failed_count += 1
                    state.failed_urls.add(url)

            state.processed_count += 1

            # Save progress periodically
            time_n = time.time()
            if time_n - state.time > 60:
                async with state.lock:
                    state.time = time_n
                    saved = state.scraped_count
                    failed = state.failed_count
                    skipped = state.skipped_count
                    state.scraped_count = 0
                    state.failed_count = 0
                    state.skipped_count = 0
                await save_progress(state)
                print(f"Progress: Scraped {saved}, Failed {failed}, Skipped {skipped}")


        except Exception as e:
            print(f"Error processing {url}: {e}")
            async with state.lock:
                state.failed_urls.add(url)
                state.failed_count += 1

async def get_session_from_selenium(driver):
    """
    Create a requests session with cookies and headers from selenium
    """
    session = requests.Session(impersonate = "chrome131")

    # Copy cookies from selenium to requests
    for cookie in driver.get_cookies():
        session.cookies.set(cookie['name'], cookie['value'])

    return session

async def fetch_sitemap(sitemap):
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

async def scrape_yummly_recipes_async(output_dir: str = 'yummly_recipes', max_concurrent: int = 5):
    """Asynchronously scrape recipes from Yummly sitemaps"""
    # Setup directories
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, 'recipes'), exist_ok=True)
    sitemap_dir = os.path.join(output_dir, 'sitemaps')

    # Initialize browser for Cloudflare bypass
    options = uc.ChromeOptions()
    options.headless = False
    driver = uc.Chrome(options=options)
    driver.get("https://yummly.com/")
    input("Press enter once you are past cloudflare...")
    session = await get_session_from_selenium(driver)
    driver.quit()

    # Load existing progress
    state = ScraperState(
        scraped_urls=set(),
        failed_urls=set(),
        output_dir=output_dir,
        session=session,
        time=time.time()
    )

    if os.path.exists(os.path.join(output_dir, 'scraped_urls.txt')):
        async with aiofiles.open(os.path.join(output_dir, 'scraped_urls.txt'), 'r') as f:
            state.scraped_urls = set((await f.read()).splitlines())

    if os.path.exists(os.path.join(output_dir, 'failed_urls.txt')):
        async with aiofiles.open(os.path.join(output_dir, 'failed_urls.txt'), 'r') as f:
            state.failed_urls = set((await f.read()).splitlines())

    # Create semaphore for rate limiting
    semaphore = asyncio.Semaphore(max_concurrent)

    # Process each sitemap
    for sitemap in os.listdir(sitemap_dir):
        print(f"Processing sitemap: {sitemap}")
        recipe_urls = await fetch_sitemap(os.path.join(sitemap_dir, sitemap))
        print(f"Found {len(recipe_urls)} URLs in sitemap")

        # Create tasks for each URL
        tasks = [
            process_url(url, state, semaphore)
            for url in recipe_urls
        ]

        # Process URLs concurrently
        await asyncio.gather(*tasks)

    # Final save of progress
    await save_progress(state)
    print(f"Final stats: Scraped {len(state.scraped_urls)}, Failed {len(state.failed_urls)}, Skipped {state.skipped_count}")

if __name__ == "__main__":
    asyncio.run(scrape_yummly_recipes_async())
