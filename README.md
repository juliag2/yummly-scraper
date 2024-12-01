# Yummly scraper
With Yummly shutting down in a few weeks, here is a scraper to download all recipes from yummly.

While building this scraper, I noticed that Yummly has a <script> tag that specifies `window.__INITIAL_STATE__`.
This object not only contains all recipe information available on the page, but also all recipe information on related recipes.
This means that the scraper can download 10-30 recipes per page, instead of just one, making it feasable to use selenium for scraping.
If the recipe has been downloaded as a "related recipe" before, it will not be downloaded again when it is encountered in the sitemap.

## Usage
1. Install chromium 131+ (ungoogled-chromium works as well).
2. Install the requirements
```bash
pip install -r requirements.txt
```
3. Download the sitemaps
I chose to download the sitemaps manually. Simply open them in a browser and save them to the `yummly_recipes/sitemaps` directory, or write a script to download them.
See sitemaps.txt for a list of sitemaps.

4. Run the scraper
```bash
python yummly_scraper.py
```
Please note that this will open a chromium window that needs to stay open, because Yummly is protected by cloudflare, and might present a captcha.
If a captcha should come up, the scraper will pause until you hit enter in the terminal.
This can probably be optimized by using requests. I might do that later.

This will download all recipes to the `yummly_recipes/recipes` folder. Each recipe is saved as a json file that takes up 20-150kb. The final size of the dataset should be around 40GB.
There is little processing done on the recipes, they are mostly retained in the format that Yummly provides them in, with the additional key 'yums' that contains the number of yums the recipe has received.