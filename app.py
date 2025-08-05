import os
import json
import re
import datetime
from urllib.parse import urlencode, quote, urlparse, parse_qsl
from urllib.request import urlopen
import requests
from bs4 import BeautifulSoup
import unicodedata
import hjson
from flask import Flask, request, jsonify, redirect

app = Flask(__name__)

# Stremio Manifest - UPDATED
MANIFEST = {
    "id": "community.prehrajto-stremio",
    "version": "0.0.1",
    "name": "Prehraj.to Stremio",
    "description": "Prehraj.to Stremio addon for movies and series.",
    "icon": "https://raw.githubusercontent.com/PeterTomascik/stremio-prehrajto-addon/main/icon.png", # TUTO NAHRAD URL ADRESOU K IKONE Z GITHUBu
    "resources": [
        "catalog",
        "stream",
        "meta" # Pridávame meta pre detaily filmov/seriálov
    ],
    "types": ["movie", "series"],
    "catalogs": [
        {
            "type": "movie",
            "id": "prehrajto_movies_popular",
            "name": "Prehraj.to Filmy (Populárne)",
            "extra": [
                {"name": "search"},
                {"name": "skip"}
            ]
        },
        {
            "type": "series",
            "id": "prehrajto_series_popular",
            "name": "Prehraj.to Seriály (Populárne)",
            "extra": [
                {"name": "search"},
                {"name": "skip"}
            ]
        }
    ],
    "behaviorHints": {
        "configurable": True,
        "noCache": True
    }
}

# Emulácia nastavení Kodi addonu
# Tieto budú nastavené cez Stremio konfiguráciu
# DEFAULT VALUES
ADDON_SETTINGS = {
    "email": os.environ.get("PREHRAJTO_EMAIL", ""), # Na Renderi to bude ako Environment Variable
    "password": os.environ.get("PREHRAJTO_PASSWORD", ""), # Na Renderi to bude ako Environment Variable
    "ls": os.environ.get("PREHRAJTO_SEARCH_LIMIT", "20") # Limit výsledkov vyhľadávania
}

# --- Utility funkcie z Kodi addonu ---
gid = {28: "Akční", 12: "Dobrodružný", 16: "Animovaný", 35: "Komedie", 80: "Krimi", 99: "Dokumentární", 18: "Drama", 10751: "Rodinný", 14: "Fantasy", 36: "Historický", 27: "Horor", 10402: "Hudební", 9648: "Mysteriózní", 10749: "Romantický", 878: "Vědeckofantastický", 10770: "Televizní film", 53: "Thriller", 10752: "Válečný", 37: "Western", 10759: "Action & Adventure", 10751: "Rodinný", 10762: "Kids", 9648: "Mysteriózní", 10763: "News", 10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics"}
headers = {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.75 Safari/537.36'} # Používame bežnejší UA

def encode(string):
    line = unicodedata.normalize('NFKD', string)
    output = ''
    for c in line:
        if not unicodedata.combining(c):
            output += c
    return output

def get_premium_session():
    email = ADDON_SETTINGS.get("email")
    password = ADDON_SETTINGS.get("password")

    if not email or not password:
        return 0, None # Not premium

    login_data = {
        "password": password,
        "email": email,
        '_submit': 'Přihlásit+se',
        'remember': 'on',
        '_do': 'login-loginForm-submit'
    }
    
    try:
        session = requests.Session()
        res = session.post("https://prehraj.to/", data=login_data, headers=headers, timeout=10)
        res.raise_for_status() # Raise an exception for HTTP errors
        
        soup = BeautifulSoup(res.content, "html.parser")
        title_element = soup.find('ul', {'class': 'header__links'}).find('span', {'class': 'color-green'})
        
        if title_element and "Premium" in title_element.text: # Check if premium status is explicitly mentioned
            print(f"Prehraj.to Premium: {title_element.text}")
            return 1, session # Premium active
        else:
            print("Prehraj.to: Premium účet neaktivní nebo nelze ověřit.")
            return 0, None # Premium not active or login failed
    except requests.exceptions.RequestException as e:
        print(f"Error during Prehraj.to login: {e}")
        return 0, None # Error, not premium


def get_link(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    file1 = ""
    file2 = ""

    # Try to find 'sources' first
    script_sources_pattern = re.compile(r'.*var sources = \[(.*?);.*', re.DOTALL)
    script_elements = soup.find_all("script", string=script_sources_pattern)

    if script_elements:
        script = script_elements[0].string
        sources_match = script_sources_pattern.findall(script)
        if sources_match:
            try:
                # Prioritize 'file' attribute if available
                file_pattern = re.compile(r'.*file: "(.*?)".*', re.DOTALL)
                file_match = file_pattern.findall(sources_match[0])
                if file_match:
                    file1 = file_match[0]
                else:
                    # Fallback to 'src' if 'file' not found
                    src_pattern = re.compile(r'.*src: "(.*?)".*', re.DOTALL)
                    src_match = src_pattern.findall(sources_match[0])
                    if src_match:
                        file1 = src_match[0]
            except Exception as e:
                print(f"Error parsing sources: {e}")
    
    # Try to find 'tracks' for subtitles
    script_tracks_pattern = re.compile(r'.*var tracks = (.*?);.*', re.DOTALL)
    script_elements = soup.find_all("script", string=script_tracks_pattern)
    
    if script_elements:
        script = script_elements[0].string
        tracks_match = script_tracks_pattern.findall(script)
        if tracks_match:
            try:
                data = hjson.loads(tracks_match[0])
                if data and isinstance(data, list) and len(data) > 0 and "src" in data[0]:
                    file2 = data[0]["src"]
            except Exception as e:
                print(f"Error parsing tracks: {e}")
                
    return file1, file2


def search_prehrajto(query, is_premium, session_cookies=None, limit=20):
    videos = []
    p = 1
    
    # Prehraj.to často má "infinite scroll" alebo "load more", takže iterujeme stránkami
    while True:
        url = f'https://prehraj.to:443/hledej/{quote(query)}?vp-page={p}'
        try:
            if is_premium and session_cookies:
                html = session_cookies.get(url, headers=headers, timeout=10).content
            else:
                html = requests.get(url, headers=headers, timeout=10).content
        except requests.exceptions.RequestException as e:
            print(f"Error fetching search results from Prehraj.to: {e}")
            break

        soup = BeautifulSoup(html, "html.parser")
        
        # Check for empty results to break early
        no_results_div = soup.find('div', class_='no-results')
        if no_results_div:
            print(f"No more search results for '{query}' on page {p}.")
            break

        titles = soup.find_all('h3', attrs={'class': 'video__title'})
        sizes = soup.find_all('div', attrs={'class': 'video__tag--size'})
        times = soup.find_all('div', attrs={'class': 'video__tag--time'})
        links = soup.find_all('a', {'class': 'video--link'})
        next_page_div = soup.find('div', {'class': 'pagination-more'}) # Used by Kodi addon to check for more pages

        if not titles: # No titles found on this page
            break

        for t, s, l, m in zip(titles, sizes, links, times):
            video_url = 'https://prehraj.to:443' + l['href']
            videos.append({
                "title": t.text.strip() + " (" + s.text.strip() + " - " + m.text.strip() + ")",
                "prehrajto_url": video_url
            })
            if len(videos) >= limit:
                break
        
        if len(videos) >= limit or not next_page_div: # If limit reached or no more "next page" indicator
            break
        
        p += 1
        # Optional: Add a small delay to avoid hammering the server
        # time.sleep(0.1) 
    return videos


# --- Stremio Routes ---

@app.route('/manifest.json')
def manifest():
    return jsonify(MANIFEST)

@app.route('/configure')
def configure():
    return jsonify(
        {
            "id": MANIFEST["id"],
            "version": MANIFEST["version"],
            "name": MANIFEST["name"],
            "description": MANIFEST["description"],
            "logo": MANIFEST["icon"],
            "background": MANIFEST["icon"], # Použijeme ikonu aj ako pozadie
            "endpoint": f"{request.url_root.rstrip('/')}/manifest.json",
            "types": ["movie", "series"],
            "dontShowOnBoard": False,
            "config": [
                {
                    "name": "email",
                    "type": "text",
                    "label": "Prehraj.to Email (pre prémiový účet)",
                    "required": False
                },
                {
                    "name": "password",
                    "type": "password",
                    "label": "Prehraj.to Heslo (pre prémiový účet)",
                    "required": False
                },
                {
                    "name": "search_limit",
                    "type": "number",
                    "label": "Limit výsledkov vyhľadávania",
                    "required": False,
                    "default": 20
                }
            ]
        }
    )


@app.route('/catalog/<type>/<id>/<config_b64>.json')
def catalog(type, id, config_b64):
    config_json = json.loads(base64_decode(config_b64))
    global ADDON_SETTINGS
    ADDON_SETTINGS["email"] = config_json.get("email", "")
    ADDON_SETTINGS["password"] = config_json.get("password", "")
    ADDON_SETTINGS["ls"] = str(config_json.get("search_limit", 20))

    extra = request.args.get('extra', '{}')
    extra_data = json.loads(extra)
    search_query = extra_data.get('search')
    skip = extra_data.get('skip', 0) # Prehraj.to pages start from 1

    metas = []
    
    # Use TMDB for popular/trending lists, then search Prehraj.to for streams
    if id == "prehrajto_movies_popular":
        tmdb_type = "movie"
        tmdb_list_type = "popular"
    elif id == "prehrajto_series_popular":
        tmdb_type = "tv"
        tmdb_list_type = "popular"
    else:
        # Fallback if catalog ID is unexpected
        tmdb_type = None
        tmdb_list_type = None

    if search_query:
        # Search directly on Prehraj.to
        premium_status, session_obj = get_premium_session()
        prehrajto_results = search_prehrajto(search_query, premium_status, session_obj, limit=int(ADDON_SETTINGS["ls"]))
        
        for item in prehrajto_results:
            metas.append({
                "id": f"pt:{item['prehrajto_url']}", # Unique ID for Stremio
                "type": type,
                "name": item["title"],
                "poster": "https://prehraj.to/favicon.ico", # Default icon
                "background": "https://prehraj.to/favicon.ico",
                "posterShape": "regular",
                "description": "Stream from Prehraj.to",
                "prehrajto_url": item["prehrajto_url"] # Store Prehraj.to URL for stream resolution
            })
    elif tmdb_type:
        # Fetch from TMDB, then attempt to find on Prehraj.to
        # This part requires more complex matching (title + year)
        # For simplicity, we'll just fetch popular from TMDB for now and point to search
        # A full TMDB integration with Prehraj.to linking would be more involved
        
        # Example for popular movies from TMDB
        page = (skip // 20) + 1 # Stremio skip is offset, TMDB pages start at 1
        tmdb_url = f'https://api.themoviedb.org/3/{tmdb_type}/{tmdb_list_type}?api_key=1f0150a5f78d4adc2407911989fdb66c&language=cs-CS&page={page}'
        try:
            tmdb_res = json.loads(urlopen(tmdb_url).read())
            for item in tmdb_res.get('results', []):
                title = item.get('title') or item.get('name')
                year = item.get('release_date', '').split('-')[0] or item.get('first_air_date', '').split('-')[0]
                
                plot = item.get('overview', '')
                genre_ids = item.get('genre_ids', [])
                genres = [gid[g] for g in genre_ids if g in gid]
                
                poster_path = item.get('poster_path')
                poster = f"http://image.tmdb.org/t/p/w342{poster_path}" if poster_path else "https://prehraj.to/favicon.ico"
                
                background_path = item.get('backdrop_path')
                background = f"https://image.tmdb.org/t/p/w1280{background_path}" if background_path else "https://prehraj.to/favicon.ico"

                meta_id = f"tmdb:{tmdb_type}:{item.get('id')}"

                metas.append({
                    "id": meta_id,
                    "type": type,
                    "name": title,
                    "poster": poster,
                    "background": background,
                    "posterShape": "regular",
                    "description": plot,
                    "genres": genres,
                    "year": year,
                    "released": item.get('release_date') or item.get('first_air_date'),
                    "imdbRating": str(item.get('vote_average'))[:3],
                    "trailer": None,
                    "videos": [],
                    "search_query": f"{title} {year}"
                })
        except Exception as e:
            print(f"Error fetching TMDB catalog for {id}: {e}")
            # In case of an error, metas will remain empty or partially filled,
            # and the function will still return jsonify({"metas": metas})
            
    return jsonify({"metas": metas})


@app.route('/meta/<type>/<id>.json')
def meta(type, id):
    # This route is used to provide detailed metadata for a specific item ID.
    # Stremio might request this if user clicks on an item in a catalog or search result.
    # The 'id' here is the one we provided in the catalog, e.g., "pt:https://prehraj.to/..." or "tmdb:movie:123"

    item_id_parts = id.split(':')
    
    if item_id_parts[0] == "pt": # If it's a Prehraj.to specific item from our search results
        prehrajto_url = id.replace("pt:", "") # Extract the Prehraj.to URL
        
        # You could fetch more detailed info from the Prehraj.to page here if needed
        # For now, we'll just return basic info, as the main goal is to provide streams
        return jsonify({
            "meta": {
                "id": id,
                "type": type,
                "name": "Prehraj.to Stream", # Or try to parse a better name from URL
                "poster": "https://prehraj.to/favicon.ico",
                "background": "https://prehraj.to/favicon.ico",
                "description": f"Stream from {prehrajto_url}",
                "posterShape": "regular",
                "videos": [] # If it's a movie, no videos array needed. For series, this would list episodes.
            }
        })
    elif item_id_parts[0] == "tmdb": # If it's a TMDB item
        tmdb_type = item_id_parts[1]
        tmdb_id = item_id_parts[2]

        if tmdb_type == 'movie':
            tmdb_url = f'https://api.themoviedb.org/3/movie/{tmdb_id}?api_key=1f0150a5f78d4adc2407911989fdb66c&language=cs-CS'
        elif tmdb_type == 'tv':
            tmdb_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}?api_key=1f0150a5f78d4adc2407911989fdb66c&language=cs-CS'
        else:
            return jsonify({"meta": None})

        try:
            tmdb_res = json.loads(urlopen(tmdb_url).read())
            
            title = tmdb_res.get('title') or tmdb_res.get('name')
            year = tmdb_res.get('release_date', '').split('-')[0] or tmdb_res.get('first_air_date', '').split('-')[0]
            plot = tmdb_res.get('overview', '')
            genre_ids = tmdb_res.get('genres', []) # For meta, genres are objects, not just IDs
            genres = [g.get('name') for g in genre_ids if g.get('name')]
            
            poster_path = tmdb_res.get('poster_path')
            poster = f"http://image.tmdb.org/t/p/w342{poster_path}" if poster_path else "https://prehraj.to/favicon.ico"
            
            background_path = tmdb_res.get('backdrop_path')
            background = f"https://image.tmdb.org/t/p/w1280{background_path}" if background_path else "https://prehraj.to/favicon.ico"

            # For series, we need to list seasons and episodes
            videos = []
            if type == 'series':
                seasons = tmdb_res.get('seasons', [])
                for season in seasons:
                    season_number = season.get('season_number')
                    if season_number is None or season.get('name') == 'Speciály':
                        continue
                    
                    # Fetch episodes for this season
                    episodes_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}?api_key=1f0150a5f78d4adc2407911989fdb66c&language=cs-CS'
                    episodes_res = json.loads(urlopen(episodes_url).read())
                    for episode in episodes_res.get('episodes', []):
                        episode_number = episode.get('episode_number')
                        if episode_number is None:
                            continue

                        episode_name = episode.get('name', f"Epizóda {episode_number}")
                        formatted_search_query = f"{title} S{str(season_number).zfill(2)}E{str(episode_number).zfill(2)}"

                        videos.append({
                            "id": f"pt:{formatted_search_query}", # Use this ID for stream resolution
                            "title": f"S{str(season_number).zfill(2)}E{str(episode_number).zfill(2)} - {episode_name}",
                            "season": season_number,
                            "episode": episode_number,
                            "released": episode.get('air_date'),
                            "overview": episode.get('overview'),
                            "thumbnail": f"http://image.tmdb.org/t/p/w185{episode.get('still_path')}" if episode.get('still_path') else poster,
                            "prehrajto_search_query": formatted_search_query # Store search query for stream
                        })
            
            return jsonify({
                "meta": {
                    "id": id,
                    "type": type,
                    "name": title,
                    "poster": poster,
                    "background": background,
                    "description": plot,
                    "genres": genres,
                    "year": year,
                    "imdbRating": str(tmdb_res.get('vote_average'))[:3],
                    "released": tmdb_res.get('release_date') or tmdb_res.get('first_air_date'),
                    "videos": videos if type == 'series' else [] # Add videos array only for series
                }
            })
        except Exception as e:
            print(f"Error fetching TMDB meta for {id}: {e}")
            return jsonify({"meta": None})
    
    return jsonify({"meta": None})


@app.route('/stream/<type>/<id>.json')
def stream(type, id):
    # This is the most crucial part: resolving the actual stream URL.
    # The 'id' here will be either "pt:https://prehraj.to/..." for direct links,
    # or "tmdb:movie:123" / "tmdb:tv:123" for TMDB items which require a search on Prehraj.to
    
    item_id_parts = id.split(':')
    prehrajto_url = None
    search_query = None

    if item_id_parts[0] == "pt":
        # Direct Prehraj.to URL provided (from catalog search results)
        prehrajto_url = id.replace("pt:", "")
    elif item_id_parts[0] == "tmdb":
        # Need to get meta first to retrieve the search query
        # This is a simplification; in a real scenario, you'd fetch TMDB meta first
        # to get the title and year/season/episode to construct the search query.
        # For now, we'll assume the 'id' might contain enough info or we'd get it from a 'meta' request beforehand
        
        # Let's assume for TMDB items, 'id' is 'tmdb:type:tmdb_id'
        # We need to search Prehraj.to using the TMDB title + year (for movies)
        # or title + SXXEXX (for episodes)
        
        if type == 'movie':
            # For movies, search by TMDB ID, get title and year, then search Prehraj.to
            tmdb_id = item_id_parts[2]
            tmdb_url = f'https://api.themoviedb.org/3/movie/{tmdb_id}?api_key=1f0150a5f78d4adc2407911989fdb66c&language=cs-CS'
            try:
                tmdb_res = json.loads(urlopen(tmdb_url).read())
                title = tmdb_res.get('title')
                year = tmdb_res.get('release_date', '').split('-')[0]
                if title and year:
                    search_query = f"{title} {year}"
            except Exception as e:
                print(f"Error fetching TMDB movie for stream: {e}")
                return jsonify({"streams": []})
        elif type == 'series' and len(item_id_parts) == 4: # Format: tmdb:series:tmdb_id:SXXEXX_query
             # For series, the ID provided to stream often contains the exact search query
             # e.g., id="pt:Movie Title S01E01"
             search_query = item_id_parts[3] # This assumes the last part of ID is the search query
        elif type == 'series':
            # If the ID for series stream is just tmdb:tv:tmdb_id, we need a separate request to get seasons/episodes
            # and let user choose. Stremio usually expects direct episode ID for series streams.
            # This logic needs refinement based on how Stremio sends episode requests.
            # For now, if it's a general series ID, we return no streams.
            print(f"Received general series ID for stream: {id}. Stremio expects specific episode ID.")
            return jsonify({"streams": []})


    streams = []
    premium_status, session_obj = get_premium_session()

    if prehrajto_url:
        # Use the provided Prehraj.to URL directly
        print(f"Resolving direct Prehraj.to URL: {prehrajto_url}")
        
        try:
            if premium_status == 1 and session_obj:
                content = session_obj.get(prehrajto_url, headers=headers, timeout=10).content
            else:
                content = requests.get(prehrajto_url, headers=headers, timeout=10).content
            
            file_url, subtitle_url = get_link(content)

            if file_url:
                if premium_status == 1 and session_obj:
                    # For premium, Prehraj.to might redirect to a direct download link on "?do=download"
                    res = session_obj.get(prehrajto_url + "?do=download", headers=headers, allow_redirects=False, timeout=10)
                    if res.status_code == 302 and 'Location' in res.headers:
                        file_url = res.headers['Location']
                    else:
                        print(f"Premium download redirect failed for {prehrajto_url}. Using original file_url.")
                
                stream_info = {
                    "name": "Prehraj.to",
                    "title": "Prehraj.to [P]",
                    "url": file_url,
                    "description": "Stream from Prehraj.to"
                }
                if subtitle_url:
                    stream_info["subtitles"] = [{"url": subtitle_url, "lang": "cze"}]
                streams.append(stream_info)
            else:
                print(f"No stream file URL found for {prehrajto_url}")

        except requests.exceptions.RequestException as e:
            print(f"Error fetching/parsing Prehraj.to URL {prehrajto_url}: {e}")
            # Optionally, notify user via Stremio log if possible
        
    elif search_query:
        # Search Prehraj.to for the given query and try to resolve the first stream
        print(f"Searching Prehraj.to for stream using query: {search_query}")
        
        prehrajto_results = search_prehrajto(search_query, premium_status, session_obj, limit=1)
        if prehrajto_results:
            first_result_url = prehrajto_results[0]["prehrajto_url"]
            print(f"Found first result for '{search_query}': {first_result_url}")
            
            try:
                if premium_status == 1 and session_obj:
                    content = session_obj.get(first_result_url, headers=headers, timeout=10).content
                else:
                    content = requests.get(first_result_url, headers=headers, timeout=10).content
                
                file_url, subtitle_url = get_link(content)

                if file_url:
                    if premium_status == 1 and session_obj:
                        res = session_obj.get(first_result_url + "?do=download", headers=headers, allow_redirects=False, timeout=10)
                        if res.status_code == 302 and 'Location' in res.headers:
                            file_url = res.headers['Location']
                        else:
                            print(f"Premium download redirect failed for {first_result_url}. Using original file_url.")
                    
                    stream_info = {
                        "name": "Prehraj.to",
                        "title": f"Prehraj.to [P] ({search_query})",
                        "url": file_url,
                        "description": "Stream from Prehraj.to"
                    }
                    if subtitle_url:
                        stream_info["subtitles"] = [{"url": subtitle_url, "lang": "cze"}]
                    streams.append(stream_info)
                else:
                    print(f"No stream file URL found for search query '{search_query}' on {first_result_url}")
            except requests.exceptions.RequestException as e:
                print(f"Error fetching/parsing Prehraj.to stream for '{search_query}': {e}")
        else:
            print(f"No Prehraj.to results found for '{search_query}'")

    return jsonify({"streams": streams})


def base64_decode(data):
    """Decode base64 string (Stremio sends config in base64)."""
    import base64
    missing_padding = len(data) % 4
    if missing_padding != 0:
        data += '='* (4 - missing_padding)
    return base64.b64decode(data).decode('utf-8')


if __name__ == '__main__':
    # This part is for local testing. Render will handle running the app.
    # Set a default value for Render.com's PORT environment variable
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
