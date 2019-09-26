import os
import os.path
import sys
import re
import webbrowser
import aiohttp
import asyncio
import struct
from collections import namedtuple


FETCH_OWNED_GAMES_URL = 'https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/' + \
    '?key={apikey}&steamid={steamid64}&format=json'

FETCH_GAME_COVER_URL = "https://www.steamgriddb.com/api/v2/grids/steam/{appid}?styles=alternate"

SGDB_API_KEY = "e7732886b6c03a829fccb9c14fff2685"

CoverResult = namedtuple("CoverResult", ["success", "appid", "urls"])


class SteamParser:
    def __init__(self, apikey, steamid64):
        self.game_cover_location = None
        self.apikey = apikey
        self.steamid64 = steamid64

    def get_steam_installpath(self):
        # for windows
        if sys.platform == 'win32':
            import winreg

            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam")
            return winreg.QueryValueEx(key, "SteamPath")[0]
        # for mac os
        elif sys.platform == 'darwin':
            return f'{os.environ["HOME"]}/Library/Application Support/Steam/'
        else:
            raise RuntimeError("Unspported Systsem: {}".format(sys.platform))

    def get_appids(self):
        steam_path = self.get_steam_installpath()
        assert os.path.isdir(steam_path), "Could not find steam install path"
        print("Steam path:", steam_path)

        if not self.apikey:
            self.apikey = self.get_steam_apikey()

        if not self.steamid64:
            self.steamid64 = self.get_steamid64()
        steamid32 = int(self.steamid64) - 76561197960265728

        # get game cover location
        steam_grid_path = os.path.join(steam_path, "userdata", str(steamid32), "config", "grid")
        if not os.path.isdir(steam_grid_path):
            os.mkdir(steam_grid_path)
        print("Steam grid path: ", steam_grid_path)
        self.game_cover_location = steam_grid_path

        missing_cover_appids = self.get_owned_games(self.apikey, self.steamid64) - \
            self.get_local_games(steam_grid_path)
        print("Total missing covers locally:", len(missing_cover_appids))

        return missing_cover_appids

    def get_owned_games(self, apikey, steamid64):

        async def _fetch_games():
            async with aiohttp.ClientSession() as session:
                resp = await session.get(FETCH_OWNED_GAMES_URL.format(apikey=apikey,
                                                                      steamid64=steamid64))
                if resp.status != 200:
                    raise RuntimeError("Can not fetch owned games")
                data = await resp.json()
                appids = [g["appid"] for g in data["response"]["games"]]
                print("Total packages in library: ", data["response"]["game_count"])

                return set(appids)

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(_fetch_games())
        return result

    def get_local_games(self, steam_grid_path):
        local_cover_appids = {int(file[:len(file)-5]) for file in os.listdir(steam_grid_path)
                              if re.match(r"^\d+p.(png|jpg)$", file)}
        print("Total local covers found:", len(local_cover_appids))
        return local_cover_appids

    def get_steam_apikey(self):
        webbrowser.open("https://steamcommunity.com/dev/apikey")
        apikey = input("Please input your steam apikey here: ")
        apikey = apikey.strip()

        assert apikey is not None and apikey != "", "Steam Api Key can not be empty!!!"

        return apikey

    def get_steamid64(self):
        steamid64 = input("Please input your steamid64 below: \n"
                          "if you do know what it is, "
                          "please check it at https://steamcommunity.com/id/{name}/?xml=1\n")
        steamid64 = steamid64.strip()

        assert steamid64 is not None and re.match(r"\d{17}", steamid64), \
            "Error steamid64"
        return steamid64


class PictureQueryClient:
    def __init__(self, apikey, cover_path, appids=list()):
        self.apikey = apikey
        self.appids = list(appids)
        self.cover_path = cover_path
        self.loop = asyncio.get_event_loop()
        self.address = []

    def run(self):
        self.loop.run_until_complete(self.fetch(self.appids))
        self.loop.close()

    async def fetch(self, appids):
        async with aiohttp.ClientSession(loop=self.loop, headers={
            "Authorization": "Bearer {}".format(SGDB_API_KEY)
        }) as session:
            no_covers, errors = await self.fetch_cover_urls(session, appids)
            print("No cover found:", no_covers)
            print("Fetch image location error:", errors)

        print("\n\nBegin to fetch images....")
        async with aiohttp.ClientSession(loop=self.loop) as session:
            errors = []
            tasks = [self.fetch_image(session, item) for item in self.address]
            for fut in asyncio.as_completed(tasks):
                result = await fut
                if result:
                    errors.append(result)

            print("Error fetch images:", errors)

    async def query_cover_for_apps(self, session, appid, retry_count=3):
        while True:
            if retry_count < 1:
                break

            retry_count = retry_count - 1
            try:
                async with session.get(FETCH_GAME_COVER_URL.format(appid=appid)) as resp:
                    print(f"Query conver for <{appid!r}>")
                    if resp.status != 200:
                        print(f"Query cover with <{appid}> failed, retry after 2 seconds")
                        await asyncio.sleep(2)
                        continue

                    data = await resp.json()
                    if not data["success"]:
                        print(f"Query cover with {appid} failed, retry after 2 seconds")
                        await asyncio.sleep(2)
                        continue

                    return CoverResult(True, appid, data["data"])
            except aiohttp.ClientConnectionError:
                print(f"Query cover with {appid} failed, retry later")
                await asyncio.sleep(2)

        return CoverResult(False, appid, None)

    async def fetch_cover_urls(self, session, appids):
        no_covers = []
        errors = []

        tasks = [self.query_cover_for_apps(session, appid) for appid in appids]
        for fut in asyncio.as_completed(tasks):
            result = await fut
            if result.success:
                if result.urls:
                    self.address.append({"appid": result.appid,
                                         "urls": sorted(result.urls, key=lambda o: o["score"],
                                                        reverse=True)})
                else:
                    no_covers.append(result.appid)
            else:
                errors.append(result.appid)

        return no_covers, errors

    async def fetch_image(self, session, item, retrycount=5):
        for info in item["urls"]:
            while retrycount > 0:
                print(f"Fetch image of game<{item['appid']}> from {info['url']} ... {retrycount}")
                retrycount -= 1
                result = await self._fetch(session, info["url"], item["appid"])
                if result:
                    return

        return item["appid"]

    async def _fetch(self, session, url, appid):
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return

                filename = os.path.join(self.cover_path, f"{appid}p{url[-4:]}")
                data = await resp.read()

                width, height = self.quick_get_image_size(data)
                if width % height == 1:
                    with open(filename, "wb") as fp:
                        fp.write(data)
                    print("Saved file to:", filename)
                    return True
                else:
                    print(f"Image size incorrect: ({width}, {height})")
                    return False
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as ex:
            print(f"Image fetch error {url}:", ex)
            return False

    def quick_get_image_size(self, data):
        height = -1
        width = -1

        size = len(data)
        # handle GIFs
        if size >= 24 and data.startswith(b'\211PNG\r\n\032\n') and data[12:16] == b'IHDR':
            try:
                width, height = struct.unpack(">LL", data[16:24])
            except struct.error:
                raise ValueError("Invalid PNG file")
        # Maybe this is for an older PNG version.
        elif size >= 16 and data.startswith(b'\211PNG\r\n\032\n'):
            # Check to see if we have the right content type
            try:
                width, height = struct.unpack(">LL", data[8:16])
            except struct.error:
                raise ValueError("Invalid PNG file")
        # handle JPEGs
        elif size >= 2 and data.startswith(b'\377\330'):
            try:
                index = 0
                size = 2
                ftype = 0
                while not 0xc0 <= ftype <= 0xcf or ftype in [0xc4, 0xc8, 0xcc]:
                    index += size
                    while data[index] == 0xff:
                        index += 1
                    ftype = data[index]
                    index += 1
                    size = struct.unpack('>H', data[index:index+2])[0]
                # We are at a SOFn block
                index += 3  # Skip `precision' byte.
                height, width = struct.unpack('>HH', data[index:index+4])
            except struct.error:
                raise ValueError("Invalid JPEG file")
        # handle JPEG2000s
        else:
            raise ValueError("Unsupported format")

        return width, height


def main(apikey, steamid64):
    steam = SteamParser(apikey, steamid64)
    appids = steam.get_appids()
    client = PictureQueryClient(SGDB_API_KEY, steam.game_cover_location, appids)
    client.run()


if __name__ == "__main__":
    apikey = None
    steamid64 = None
    if len(sys.argv) > 2:
        apikey, steamid64 = sys.argv[1], sys.argv[2]
    main(apikey, steamid64)
