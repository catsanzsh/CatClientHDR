import os, sys, json, shutil, zipfile, threading, queue, hashlib, time, functools
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import re
import subprocess
import uuid as uuidlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# --- Constants ---
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

# --- Directory Setup ---
def get_minecraft_dir():
    if os.name == 'nt':
        return Path(os.getenv('APPDATA', '')) / '.minecraft'
    elif sys.platform == 'darwin':
        return Path.home() / "Library/Application Support/minecraft"
    else:
        return Path.home() / ".minecraft"

mc_dir = get_minecraft_dir()
mc_dir.mkdir(exist_ok=True)

VERSIONS_DIR = mc_dir / "versions"
ASSETS_DIR = mc_dir / "assets"
MODPACKS_DIR = mc_dir / "modpacks"
LIBRARIES_DIR = mc_dir / "libraries"
CACHE_DIR = mc_dir / "cache"

VERSIONS_DIR.mkdir(exist_ok=True)
MODPACKS_DIR.mkdir(exist_ok=True)
(ASSETS_DIR / "indexes").mkdir(exist_ok=True, parents=True)
(ASSETS_DIR / "objects").mkdir(exist_ok=True, parents=True)
LIBRARIES_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

# URLs
VERSION_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
FALLBACK_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
ASSET_BASE_URL = "http://resources.download.minecraft.net/"
LIBRARIES_BASE_URL = "https://libraries.minecraft.net/"
FORGE_MAVEN_URL = "https://maven.minecraftforge.net/"
TLMODS_BASE_URL = "https://tlmods.org"

# --- Download Cache System ---
class DownloadCache:
    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.cache_index_file = self.cache_dir / "cache_index.json"
        self.cache_index = self._load_cache_index()
        self.cache_lock = threading.Lock()
    
    def _load_cache_index(self):
        if self.cache_index_file.exists():
            try:
                with open(self.cache_index_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                print("Cache index corrupt, creating new one")
        return {"url_to_file": {}, "file_to_url": {}}
    
    def _save_cache_index(self):
        with self.cache_lock:
            try:
                with open(self.cache_index_file, 'w') as f:
                    json.dump(self.cache_index, f)
            except IOError as e:
                print(f"Failed to save cache index: {e}")
    
    def get_cache_path(self, url):
        with self.cache_lock:
            cache_filename = self.cache_index["url_to_file"].get(url)
            if cache_filename:
                cache_path = self.cache_dir / cache_filename
                if cache_path.exists():
                    return cache_path
        return None
    
    def add_to_cache(self, url, file_path):
        url_hash = hashlib.md5(url.encode()).hexdigest()
        cache_filename = f"{url_hash}{Path(file_path).suffix}"
        cache_path = self.cache_dir / cache_filename
        try:
            shutil.copy2(file_path, cache_path)
            with self.cache_lock:
                self.cache_index["url_to_file"][url] = cache_filename
                self.cache_index["file_to_url"][cache_filename] = url
                self._save_cache_index()
            return cache_path
        except IOError as e:
            print(f"Failed to cache file {url}: {e}")
            return None

download_cache = DownloadCache(CACHE_DIR)

# --- Concurrent Download Manager ---
class DownloadManager:
    def __init__(self, max_workers=10):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.futures = []
        self.results = {}
        self.download_count = 0
        self.failed_count = 0
        self.cached_count = 0
        self.total_count = 0
        self.status_callback = None
        self.lock = threading.Lock()
    
    def set_status_callback(self, callback):
        self.status_callback = callback
    
    def update_status(self, message):
        if self.status_callback:
            self.status_callback(message)
    
    def download_file(self, url, dest_path, description="file", check_cache=True):
        self.total_count += 1
        future = self.executor.submit(
            self._download_file_worker, url, dest_path, description, check_cache
        )
        self.futures.append(future)
        return future
    
    def _download_file_worker(self, url, dest_path, description, check_cache):
        dest_path = Path(dest_path)
        if dest_path.exists():
            with self.lock:
                self.cached_count += 1
                self.update_status(f"Already exists: {os.path.basename(dest_path)} ({self.download_count + self.cached_count}/{self.total_count})")
            return {"success": True, "path": dest_path, "source": "existing"}
        
        if check_cache:
            cached_path = download_cache.get_cache_path(url)
            if cached_path:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(cached_path, dest_path)
                    with self.lock:
                        self.cached_count += 1
                        self.update_status(f"Using cached: {os.path.basename(dest_path)} ({self.download_count + self.cached_count}/{self.total_count})")
                    return {"success": True, "path": dest_path, "source": "cache"}
                except IOError:
                    pass
        
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        try:
            with self.lock:
                self.update_status(f"Downloading: {os.path.basename(dest_path)} ({self.download_count + self.cached_count}/{self.total_count})")
            with urllib.request.urlopen(req) as response, open(dest_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
            download_cache.add_to_cache(url, dest_path)
            with self.lock:
                self.download_count += 1
                self.update_status(f"Downloaded: {os.path.basename(dest_path)} ({self.download_count + self.cached_count}/{self.total_count})")
            return {"success": True, "path": dest_path, "source": "download"}
        except urllib.error.HTTPError as e:
            with self.lock:
                self.failed_count += 1
            return {"success": False, "error": f"HTTP Error: {e.code} {e.reason}", "url": url, "path": dest_path}
        except Exception as e:
            with self.lock:
                self.failed_count += 1
            return {"success": False, "error": str(e), "url": url, "path": dest_path}
    
    def wait_for_downloads(self):
        results = []
        for future in self.futures:
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                results.append({"success": False, "error": str(e)})
        self.futures = []
        return results
    
    def download_multiple(self, downloads):
        for download in downloads:
            self.download_file(
                download['url'], 
                download['dest_path'], 
                download.get('description', 'file'),
                download.get('check_cache', True)
            )
        return self.wait_for_downloads()
    
    def reset_counters(self):
        with self.lock:
            self.download_count = 0
            self.failed_count = 0
            self.cached_count = 0
            self.total_count = 0

download_manager = DownloadManager()

# --- Version Manifest Handling ---
class VersionManager:
    def __init__(self, mc_dir):
        self.mc_dir = Path(mc_dir)
        self.versions_dir = self.mc_dir / "versions"
        self.version_manifest_path = self.mc_dir / "version_manifest_v2.json"
        self.version_manifest = self._load_or_download_manifest()
        self.versions_cache = {}
    
    def _load_or_download_manifest(self):
        if not self.version_manifest_path.exists():
            try:
                self._download_manifest()
            except Exception as e:
                print(f"Failed to download v2 manifest: {e}")
                try:
                    self.version_manifest_path = self.mc_dir / "version_manifest.json"
                    self._download_manifest(use_v2=False)
                except Exception as e:
                    raise Exception(f"Could not download any version manifest: {e}")
        try:
            with open(self.version_manifest_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            raise Exception(f"Could not parse version manifest: {e}")
    
    def _download_manifest(self, use_v2=True):
        url = VERSION_MANIFEST_URL if use_v2 else FALLBACK_MANIFEST_URL
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req) as response, open(self.version_manifest_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
    
    def get_all_versions(self):
        return {v['id']: v['url'] for v in self.version_manifest.get('versions', [])}
    
    def get_release_versions(self):
        return sorted([v['id'] for v in self.version_manifest.get('versions', []) if v['type'] == 'release'], reverse=True)
    
    def get_snapshot_versions(self):
        return sorted([v['id'] for v in self.version_manifest.get('versions', []) if v['type'] == 'snapshot'], reverse=True)
    
    def get_custom_versions(self):
        all_versions = self.get_all_versions()
        custom_versions = [item.name for item in self.versions_dir.iterdir() if item.is_dir() and item.name not in all_versions]
        return sorted(custom_versions, reverse=True)
    
    def load_version_json(self, version_id):
        if version_id in self.versions_cache:
            return self.versions_cache[version_id]
        version_folder = self.versions_dir / version_id
        version_json_path = version_folder / f"{version_id}.json"
        if not version_json_path.exists():
            all_versions = self.get_all_versions()
            if version_id not in all_versions:
                raise Exception(f"Version '{version_id}' not found in Mojang manifest.")
            version_url = all_versions[version_id]
            version_folder.mkdir(exist_ok=True)
            req = urllib.request.Request(version_url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req) as response, open(version_json_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
        try:
            with open(version_json_path, 'r') as f:
                version_data = json.load(f)
                self.versions_cache[version_id] = version_data
                return version_data
        except Exception as e:
            raise Exception(f"Failed to load version JSON for {version_id}: {e}")

version_manager = VersionManager(mc_dir)

# --- Secure Account Manager ---
class AccountManager:
    def __init__(self, mc_dir):
        self.mc_dir = Path(mc_dir)
        self.accounts_file = self.mc_dir / "launcher_accounts.json"
        self.accounts = self._load_accounts()
    
    def _load_accounts(self):
        if self.accounts_file.exists():
            try:
                with open(self.accounts_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                print(f"Warning: Could not parse {self.accounts_file}. Starting with empty accounts.")
            except Exception as e:
                print(f"Warning: Error loading accounts: {e}")
        return []
    
    def save_accounts(self):
        try:
            with open(self.accounts_file, 'w') as f:
                json.dump(self.accounts, f, indent=4)
        except Exception as e:
            print(f"Error saving accounts: {e}")
    
    def add_account(self, acc_type, email_username, password_token=None):
        if not email_username:
            return False
        offline_uuid = str(uuidlib.uuid3(uuidlib.NAMESPACE_DNS, email_username))
        if acc_type == "tlauncher":
            hashed_pwd = self._simple_obfuscate(password_token) if password_token else ""
            acc = {"type": "tlauncher", "username": email_username, "password": hashed_pwd, "uuid": offline_uuid, "token": "null"}
        elif acc_type == "offline":
            acc = {"type": "offline", "username": email_username, "uuid": offline_uuid, "token": "null"}
        elif acc_type == "microsoft":
            acc = {"type": "microsoft", "username": email_username, "uuid": offline_uuid, "token": (password_token or "0")}
        else:
            print(f"Unknown account type: {acc_type}")
            return False
        for i, existing_acc in enumerate(self.accounts):
            if existing_acc.get("type") == acc_type and existing_acc.get("username") == email_username:
                self.accounts[i] = acc
                self.save_accounts()
                return True
        self.accounts.append(acc)
        self.save_accounts()
        return True
    
    def get_accounts(self):
        return self.accounts
    
    def get_account(self, index):
        if 0 <= index < len(self.accounts):
            return self.accounts[index]
        return None
    
    def _simple_obfuscate(self, text):
        if not text:
            return ""
        key = "CatClientKey"
        result = [chr(ord(char) ^ ord(key[i % len(key)])) for i, char in enumerate(text)]
        return "".join(result)
    
    def _simple_deobfuscate(self, obfuscated):
        return self._simple_obfuscate(obfuscated)
    
    def get_auth_details(self, account):
        if account.get("type") == "tlauncher" and account.get("password"):
            auth_details = account.copy()
            auth_details["password"] = self._simple_deobfuscate(account["password"])
            return auth_details
        return account

account_manager = AccountManager(mc_dir)

# --- Game Installation Functions ---
def install_version(version_id, status_callback=None):
    if status_callback:
        status_callback(f"Installing version: {version_id}...")
        download_manager.set_status_callback(status_callback)
    download_manager.reset_counters()
    try:
        version_data = version_manager.load_version_json(version_id)
    except Exception as e:
        raise Exception(f"Failed to load version data: {e}")
    parent_id = version_data.get("inheritsFrom")
    parent_data = {}
    if parent_id:
        if status_callback:
            status_callback(f"Version {version_id} inherits from {parent_id}. Installing parent...")
        try:
            install_version(parent_id, status_callback)
            parent_data = version_manager.load_version_json(parent_id)
        except Exception as e:
            raise Exception(f"Failed to install parent version {parent_id}: {e}")
    version_folder = VERSIONS_DIR / version_id
    version_jar_path = version_folder / f"{version_id}.jar"
    client_info = version_data.get("downloads", {}).get("client")
    if client_info and not version_jar_path.exists():
        client_url = client_info.get("url")
        if client_url:
            if status_callback:
                status_callback(f"Downloading client JAR for {version_id}...")
            result = download_manager.download_file(client_url, version_jar_path, f"client JAR ({version_id})").result()
            if not result["success"]:
                print(f"Warning: Failed to download client JAR for {version_id}: {result['error']}")
    libraries = version_data.get("libraries", []) + parent_data.get("libraries", [])
    if status_callback:
        status_callback(f"Checking libraries for {version_id}...")
    lib_downloads = []
    for lib in libraries:
        if not _check_library_rules(lib):
            continue
        artifact = lib.get("downloads", {}).get("artifact")
        if artifact and artifact.get("path"):
            lib_path = LIBRARIES_DIR / artifact["path"]
            if not lib_path.exists():
                lib_url = artifact.get("url") or f"{FORGE_MAVEN_URL if 'forge' in lib.get('name', '').lower() else LIBRARIES_BASE_URL}{artifact['path']}"
                lib_downloads.append({'url': lib_url, 'dest_path': lib_path, 'description': f"library ({Path(lib_path).name})"})
        natives_info = lib.get("natives")
        classifiers = lib.get("downloads", {}).get("classifiers", {})
        if natives_info and classifiers:
            os_key_map = {'win32': 'windows', 'darwin': 'osx', 'linux': 'linux'}
            native_os = os_key_map.get(sys.platform)
            if native_os:
                arch = '64'
                native_key = natives_info.get(native_os, '').replace("${arch}", arch)
                if native_key and native_key in classifiers:
                    native_artifact = classifiers[native_key]
                    if native_artifact.get("path"):
                        native_path = LIBRARIES_DIR / native_artifact["path"]
                        if not native_path.exists():
                            native_url = native_artifact.get("url") or f"{FORGE_MAVEN_URL if 'forge' in lib.get('name', '').lower() else LIBRARIES_BASE_URL}{native_artifact['path']}"
                            lib_downloads.append({'url': native_url, 'dest_path': native_path, 'description': f"native ({Path(native_path).name})"})
    if lib_downloads:
        if status_callback:
            status_callback(f"Downloading {len(lib_downloads)} libraries for {version_id}...")
        results = download_manager.download_multiple(lib_downloads)
        natives_dir = version_folder / "natives"
        natives_dir.mkdir(exist_ok=True)
        for lib, result in zip(lib_downloads, results):
            if result["success"] and "native" in lib["description"]:
                try:
                    _extract_natives(result["path"], natives_dir)
                except Exception as e:
                    print(f"Warning: Failed to extract natives from {result['path']}: {e}")
    asset_index_info = version_data.get("assetIndex") or parent_data.get("assetIndex")
    if asset_index_info and asset_index_info.get("id") and asset_index_info.get("url"):
        idx_id = asset_index_info["id"]
        idx_url = asset_index_info["url"]
        idx_dest = ASSETS_DIR / "indexes" / f"{idx_id}.json"
        if not idx_dest.exists():
            if status_callback:
                status_callback(f"Downloading asset index {idx_id}...")
            result = download_manager.download_file(idx_url, idx_dest, f"asset index ({idx_id})").result()
            if not result["success"]:
                raise Exception(f"Failed to download asset index: {result['error']}")
        try:
            with open(idx_dest, 'r') as f:
                idx_data = json.load(f)
            if idx_data and "objects" in idx_data:
                if status_callback:
                    status_callback(f"Checking assets for index {idx_id}...")
                asset_downloads = []
                for asset_name, info in idx_data["objects"].items():
                    hash_val = info.get("hash")
                    if hash_val:
                        subdir = hash_val[:2]
                        asset_path = ASSETS_DIR / "objects" / subdir / hash_val
                        if not asset_path.exists():
                            asset_downloads.append({'url': f"{ASSET_BASE_URL}{subdir}/{hash_val}", 'dest_path': asset_path, 'description': f"asset ({asset_name})"})
                if asset_downloads:
                    if status_callback:
                        status_callback(f"Downloading {len(asset_downloads)} assets for {version_id}...")
                    download_manager.download_multiple(asset_downloads)
        except Exception as e:
            print(f"Warning: Error processing assets for index {idx_id}: {e}")
    download_manager.wait_for_downloads()
    if status_callback:
        status_callback(f"Version {version_id} installation complete.")

def _check_library_rules(lib):
    rules = lib.get("rules", [])
    if not rules:
        return True
    allowed = False
    for rule in rules:
        action = rule.get("action")
        os_rule = rule.get("os", {})
        if action == "allow":
            if not os_rule or os_rule.get("name") in [sys.platform, {'win32': 'windows', 'darwin': 'osx', 'linux': 'linux'}.get(sys.platform)]:
                allowed = True
                break
        elif action == "disallow":
            if not os_rule or os_rule.get("name") in [sys.platform, {'win32': 'windows', 'darwin': 'osx', 'linux': 'linux'}.get(sys.platform)]:
                allowed = False
                break
    return allowed

def _extract_natives(native_path, natives_dir):
    with zipfile.ZipFile(native_path, 'r') as zf:
        exclude_prefixes = ["META-INF/"]
        for member in zf.namelist():
            if any(member.startswith(prefix) for prefix in exclude_prefixes) or member.endswith('/'):
                continue
            zf.extract(member, natives_dir)

# --- Server Management Functions ---
def download_server_jar(version_id, status_callback=None):
    server_dir = mc_dir / "server"
    server_dir.mkdir(exist_ok=True)
    server_jar = server_dir / f"server-{version_id}.jar"
    if server_jar.exists():
        if status_callback:
            status_callback(f"Server JAR for {version_id} already exists.")
        return server_jar
    version_data = version_manager.load_version_json(version_id)
    server_info = version_data.get("downloads", {}).get("server")
    if not server_info or not server_info.get("url"):
        raise Exception(f"No server JAR available for version {version_id}")
    if status_callback:
        status_callback(f"Downloading server JAR for {version_id}...")
    result = download_manager.download_file(server_info["url"], server_jar, f"server JAR ({version_id})").result()
    if not result["success"]:
        raise Exception(f"Failed to download server JAR: {result['error']}")
    return server_jar

def launch_server(version_id, ram_mb=2048, java_path="java", status_callback=None):
    server_dir = mc_dir / "server"
    server_jar = download_server_jar(version_id, status_callback)
    if not (server_dir / "eula.txt").exists():
        with open(server_dir / "eula.txt", "w") as f:
            f.write("eula=true\n")
    command = [java_path, f"-Xmx{ram_mb}M", "-jar", str(server_jar), "nogui"]
    if status_callback:
        status_callback(f"Launching server for {version_id}...")
    try:
        process = subprocess.Popen(command, cwd=str(server_dir))
        if status_callback:
            status_callback(f"Server {version_id} launched! (PID: {process.pid})")
        return process
    except Exception as e:
        raise Exception(f"Failed to launch server: {e}")

# --- Game Launch Functions ---
def launch_game(version_id, account, ram_mb=1024, java_path="java", game_dir=None, server_ip=None, port=None, jvm_args_custom=None, game_args_custom=None, status_callback=None):
    if status_callback:
        status_callback(f"Preparing to launch {version_id}...")
    effective_game_dir = game_dir if game_dir and Path(game_dir).is_dir() else mc_dir
    try:
        install_version(version_id, status_callback)
    except Exception as e:
        raise Exception(f"Failed to install version {version_id}: {e}")
    version_data = version_manager.load_version_json(version_id)
    parent_data = {}
    if inherits_from := version_data.get("inheritsFrom"):
        try:
            parent_data = version_manager.load_version_json(inherits_from)
        except Exception as e:
            print(f"Warning: Could not load parent version {inherits_from}: {e}")
    main_class = version_data.get("mainClass") or parent_data.get("mainClass")
    if not main_class:
        raise Exception(f"Could not determine main class for {version_id}")
    classpath = _build_classpath(version_id, version_data, parent_data)
    jvm_args, game_args = _build_launch_args(version_id, version_data, parent_data, account, ram_mb, effective_game_dir, server_ip, port)
    if jvm_args_custom:
        jvm_args.extend(jvm_args_custom)
    if game_args_custom:
        game_args.extend(game_args_custom)
    command = [java_path] + jvm_args + [main_class] + game_args
    print("\n--- Launch Command ---")
    print("Java Path:", command[0])
    print("Main Class:", main_class)
    print("JVM Args Count:", len(jvm_args))
    print("Game Args Count:", len(game_args))
    print("----------------------\n")
    if status_callback:
        status_callback(f"Launching Minecraft {version_id}...")
    try:
        process = subprocess.Popen(command, cwd=str(effective_game_dir))
        if status_callback:
            status_callback(f"Minecraft {version_id} launched! (PID: {process.pid})")
    except FileNotFoundError:
        raise Exception(f"Java executable not found at '{java_path}'")
    except Exception as e:
        raise Exception(f"Failed to launch Minecraft: {e}")

def _build_classpath(version_id, version_data, parent_data):
    classpath = set()
    libraries = version_data.get("libraries", []) + parent_data.get("libraries", [])
    for lib in libraries:
        if not _check_library_rules(lib):
            continue
        artifact = lib.get("downloads", {}).get("artifact")
        if artifact and artifact.get("path"):
            lib_file = LIBRARIES_DIR / artifact["path"]
            if lib_file.exists():
                classpath.add(str(lib_file.absolute()))
    version_folder = VERSIONS_DIR / version_id
    version_jar = version_folder / f"{version_id}.jar"
    if version_jar.exists():
        classpath.add(str(version_jar.absolute()))
    elif parent_id := version_data.get("inheritsFrom"):
        parent_jar = VERSIONS_DIR / parent_id / f"{parent_id}.jar"
        if parent_jar.exists():
            classpath.add(str(parent_jar.absolute()))
    return os.pathsep.join(classpath)

def _build_launch_args(version_id, version_data, parent_data, account, ram_mb, game_dir, server_ip, port):
    version_folder = VERSIONS_DIR / version_id
    natives_dir = version_folder / "natives"
    natives_dir.mkdir(exist_ok=True)
    replacements = {
        "${auth_player_name}": account.get("username", "Player"),
        "${version_name}": version_id,
        "${game_directory}": str(game_dir),
        "${assets_root}": str(ASSETS_DIR.absolute()),
        "${assets_index_name}": (version_data.get("assetIndex") or parent_data.get("assetIndex", {})).get("id", "legacy"),
        "${auth_uuid}": account.get("uuid", "invalid-uuid"),
        "${auth_access_token}": account.get("token", "0"),
        "${user_type}": "msa" if account.get("type") == "microsoft" else "legacy",
        "${version_type}": version_data.get("type", "release"),
        "${natives_directory}": str(natives_dir.absolute()),
        "${launcher_name}": "CatClient",
        "${launcher_version}": "2.0",
        "${classpath_separator}": os.pathsep,
        "${library_directory}": str(LIBRARIES_DIR.absolute()),
    }
    jvm_args = [
        f"-Xmx{ram_mb}M",
        "-XX:+UnlockExperimentalVMOptions",
        "-XX:+UseG1GC",
        "-XX:G1NewSizePercent=20",
        "-XX:G1ReservePercent=20",
        "-XX:MaxGCPauseMillis=50",
        "-XX:G1HeapRegionSize=32M",
        f"-Djava.library.path={natives_dir.absolute()}",
    ]
    args_data = version_data.get("arguments", {})
    parent_args_data = parent_data.get("arguments", {})
    raw_jvm_args = parent_args_data.get("jvm", []) + args_data.get("jvm", [])
    for arg in raw_jvm_args:
        if isinstance(arg, str):
            temp_arg = arg
            for key, value in replacements.items():
                temp_arg = temp_arg.replace(key, str(value))
            jvm_args.append(temp_arg)
        elif isinstance(arg, dict) and "rules" in arg:
            if _check_argument_rules(arg.get("rules", [])):
                value = arg.get("value")
                if isinstance(value, list):
                    for v in value:
                        temp_arg = v
                        for key, val in replacements.items():
                            temp_arg = temp_arg.replace(key, str(val))
                        jvm_args.append(temp_arg)
                elif isinstance(value, str):
                    temp_arg = value
                    for key, val in replacements.items():
                        temp_arg = temp_arg.replace(key, str(val))
                    jvm_args.append(temp_arg)
    classpath = _build_classpath(version_id, version_data, parent_data)
    jvm_args.extend(["-cp", classpath])
    game_args = []
    raw_game_args = parent_args_data.get("game", []) + args_data.get("game", [])
    if not raw_game_args:
        legacy_args_str = version_data.get("minecraftArguments") or parent_data.get("minecraftArguments", "")
        if legacy_args_str:
            raw_game_args = legacy_args_str.split()
    for arg in raw_game_args:
        if isinstance(arg, str):
            temp_arg = arg
            for key, value in replacements.items():
                temp_arg = temp_arg.replace(key, str(value))
            game_args.append(temp_arg)
        elif isinstance(arg, dict) and "rules" in arg:
            if _check_argument_rules(arg.get("rules", [])):
                value = arg.get("value")
                if isinstance(value, list):
                    for v in value:
                        temp_arg = v
                        for key, val in replacements.items():
                            temp_arg = temp_arg.replace(key, str(val))
                        game_args.append(temp_arg)
                elif isinstance(value, str):
                    temp_arg = value
                    for key, val in replacements.items():
                        temp_arg = temp_arg.replace(key, str(val))
                    game_args.append(temp_arg)
    if server_ip:
        game_args.extend(["--server", server_ip])
        if port:
            game_args.extend(["--port", str(port)])
    return jvm_args, game_args

def _check_argument_rules(rules):
    if not rules:
        return True
    for rule in rules:
        action = rule.get("action")
        os_rule = rule.get("os", {})
        features = rule.get("features", {})
        if features:
            continue
        if action == "allow":
            if not os_rule or os_rule.get("name") in [sys.platform, {'win32': 'windows', 'darwin': 'osx', 'linux': 'linux'}.get(sys.platform)]:
                return True
        elif action == "disallow":
            if not os_rule or os_rule.get("name") in [sys.platform, {'win32': 'windows', 'darwin': 'osx', 'linux': 'linux'}.get(sys.platform)]:
                return False
    return False

# --- GUI Class ---
class CatClientApp:
    def __init__(self, root):
        self.root = root
        self.root.title("CatClient (Lunar-Like) v2.0")
        self.root.geometry("700x650")
        self._apply_styling()
        self._create_account_frame()
        self._create_version_frame()
        self._create_mod_frame()
        self._create_options_frame()
        self._create_server_frame()
        self._create_status_bar()
        self._create_launch_button()
        self.refresh_account_list()
        self._load_versions()
    
    def _apply_styling(self):
        style = ttk.Style()
        try:
            if os.name == 'nt':
                style.theme_use('vista')
            else:
                style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure("TLabel", padding=3)
        style.configure("TButton", padding=6)
        style.configure("Accent.TButton", font=('Segoe UI', 11, 'bold'))
        style.configure("TFrame", padding=5)
        style.configure("TLabelframe", padding=8)
        style.configure("TLabelframe.Label", font=('Segoe UI', 9, 'bold'))
    
    def _create_account_frame(self):
        frame = ttk.LabelFrame(self.root, text="Accounts")
        frame.pack(fill="x", padx=10, pady=5)
        self.acct_type_var = tk.StringVar(value="tlauncher")
        ttk.Radiobutton(frame, text="TLauncher", variable=self.acct_type_var, value="tlauncher").grid(row=0, column=0, sticky="w", padx=5)
        ttk.Radiobutton(frame, text="Offline", variable=self.acct_type_var, value="offline").grid(row=0, column=1, sticky="w", padx=5)
        ttk.Radiobutton(frame, text="Microsoft (Demo)", variable=self.acct_type_var, value="microsoft", state="disabled").grid(row=0, column=2, sticky="w", padx=5)
        ttk.Label(frame, text="Username/Email:").grid(row=1, column=0, padx=5, pady=3, sticky="e")
        self.username_entry = ttk.Entry(frame, width=30)
        self.username_entry.grid(row=1, column=1, columnspan=2, padx=5, pady=3, sticky="we")
        ttk.Label(frame, text="Password/Token:").grid(row=2, column=0, padx=5, pady=3, sticky="e")
        self.password_entry = ttk.Entry(frame, width=30, show="*")
        self.password_entry.grid(row=2, column=1, columnspan=2, padx=5, pady=3, sticky="we")
        ttk.Button(frame, text="Add / Update Account", command=self.on_add_account).grid(row=1, column=3, rowspan=2, padx=10, pady=5, sticky="ns")
        ttk.Label(frame, text="(Optional for Offline, Needed for TLauncher)").grid(row=3, column=1, columnspan=2, sticky="w", padx=5)
        ttk.Separator(frame, orient='horizontal').grid(row=5, column=0, columnspan=4, sticky="ew", pady=10)
        ttk.Label(frame, text="Select Account:").grid(row=6, column=0, padx=5, pady=5, sticky="e")
        self.account_var = tk.StringVar()
        self.account_combo = ttk.Combobox(frame, textvariable=self.account_var, state="readonly", width=40)
        self.account_combo.grid(row=6, column=1, columnspan=3, padx=5, pady=5, sticky="we")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=1)
    
    def _create_version_frame(self):
        frame = ttk.LabelFrame(self.root, text="Game Version")
        frame.pack(fill="x", padx=10, pady=5)
        self.version_var = tk.StringVar()
        self.version_combo = ttk.Combobox(frame, textvariable=self.version_var, state="readonly", width=50)
        self.version_combo.grid(row=0, column=0, padx=5, pady=5, sticky="we")
        frame.columnconfigure(0, weight=1)
    
    def _create_mod_frame(self):
        frame = ttk.LabelFrame(self.root, text="Mods")
        frame.pack(fill="x", padx=10, pady=5)
        self.mod_vars = {}
        mods = ["OptiFine", "Sodium", "Iris Shaders"]  # Example mods; expand as needed
        for i, mod_name in enumerate(mods):
            var = tk.BooleanVar(value=False)
            self.mod_vars[mod_name] = var
            ttk.Checkbutton(frame, text=mod_name, variable=var).grid(row=i // 3, column=i % 3, padx=5, pady=3, sticky="w")
    
    def _create_options_frame(self):
        frame = ttk.LabelFrame(self.root, text="Launch Options")
        frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(frame, text="Max RAM (MB):").grid(row=0, column=0, padx=5, pady=3, sticky="e")
        self.ram_var = tk.IntVar(value=4096)
        self.ram_slider = ttk.Scale(frame, from_=512, to=32768, variable=self.ram_var, orient="horizontal", length=200)
        self.ram_slider.grid(row=0, column=1, padx=5, pady=3, sticky="we")
        self.ram_label = ttk.Label(frame, textvariable=tk.StringVar(value="4096 MB"))
        self.ram_label.grid(row=0, column=2, padx=5, pady=3, sticky="w")
        self.ram_slider.bind("<Motion>", self._update_ram_label)
        ttk.Label(frame, text="Java Path:").grid(row=1, column=0, padx=5, pady=3, sticky="e")
        self.java_entry = ttk.Entry(frame, width=40)
        self.java_entry.insert(0, self._find_java())
        self.java_entry.grid(row=1, column=1, padx=5, pady=3, sticky="we")
        ttk.Button(frame, text="Browse...", command=self._browse_java).grid(row=1, column=2, padx=5)
        ttk.Label(frame, text="Custom JVM Args:").grid(row=2, column=0, padx=5, pady=3, sticky="e")
        self.jvm_args_entry = ttk.Entry(frame, width=40)
        self.jvm_args_entry.grid(row=2, column=1, columnspan=2, padx=5, pady=3, sticky="we")
        ttk.Label(frame, text="Custom Game Args:").grid(row=3, column=0, padx=5, pady=3, sticky="e")
        self.game_args_entry = ttk.Entry(frame, width=40)
        self.game_args_entry.grid(row=3, column=1, columnspan=2, padx=5, pady=3, sticky="we")
        frame.columnconfigure(1, weight=1)
    
    def _create_server_frame(self):
        frame = ttk.LabelFrame(self.root, text="Server Management")
        frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(frame, text="Server Version:").grid(row=0, column=0, padx=5, pady=3, sticky="e")
        self.server_version_var = tk.StringVar()
        self.server_version_combo = ttk.Combobox(frame, textvariable=self.server_version_var, state="readonly", width=30)
        self.server_version_combo.grid(row=0, column=1, padx=5, pady=3, sticky="we")
        ttk.Button(frame, text="Launch Server", command=self.on_launch_server).grid(row=0, column=2, padx=5, pady=3)
        frame.columnconfigure(1, weight=1)
    
    def _create_status_bar(self):
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Frame(self.root, relief=tk.SUNKEN, padding="2 2 2 2")
        status_bar.pack(side=tk.BOTTOM, fill="x")
        self.status_label = ttk.Label(status_bar, textvariable=self.status_var)
        self.status_label.pack(side=tk.LEFT)
    
    def _create_launch_button(self):
        self.launch_btn = ttk.Button(self.root, text="Launch Game", command=self.on_launch, style="Accent.TButton")
        self.launch_btn.pack(pady=15, ipadx=20, ipady=10)
    
    def _find_java(self):
        java_exe = "java.exe" if os.name == 'nt' else "java"
        java_home = os.environ.get("JAVA_HOME")
        if java_home and Path(java_home, "bin", java_exe).is_file():
            return str(Path(java_home, "bin", java_exe))
        java_path = shutil.which(java_exe)
        return java_path or "java"
    
    def _browse_java(self):
        file_types = [("Java Executable", "java.exe")] if os.name == 'nt' else [("Java Executable", "java")]
        filename = filedialog.askopenfilename(title="Select Java Executable", filetypes=file_types)
        if filename:
            self.java_entry.delete(0, tk.END)
            self.java_entry.insert(0, filename)
    
    def _update_ram_label(self, event=None):
        ram_val = round(self.ram_var.get() / 512) * 512
        self.ram_var.set(ram_val)
        self.ram_label.config(text=f"{ram_val} MB")
    
    def _load_versions(self):
        try:
            release_versions = version_manager.get_release_versions()
            snapshot_versions = version_manager.get_snapshot_versions()
            custom_versions = version_manager.get_custom_versions()
            combined_list = release_versions + snapshot_versions + custom_versions
            self.version_combo['values'] = combined_list
            self.server_version_combo['values'] = release_versions  # Servers typically use release versions
            if release_versions:
                self.version_combo.set(release_versions[0])
                self.server_version_combo.set(release_versions[0])
            elif combined_list:
                self.version_combo.set(combined_list[0])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load versions: {e}")
    
    def set_status(self, message, color="black"):
        self.root.after(0, self._update_status_ui, message, color)
    
    def _update_status_ui(self, message, color):
        self.status_var.set(message)
        self.status_label.config(foreground=color)
    
    def refresh_account_list(self):
        accounts = account_manager.get_accounts()
        display_names = [f"{acc.get('type','N/A').capitalize()}: {acc.get('username','Unknown')}" for acc in accounts]
        self.account_combo['values'] = display_names
        if display_names:
            current = self.account_var.get()
            if current in display_names:
                self.account_combo.set(current)
            else:
                self.account_combo.current(0)
        else:
            self.account_combo.set('')
    
    def on_add_account(self):
        acc_type = self.acct_type_var.get()
        user = self.username_entry.get().strip()
        pwd = self.password_entry.get().strip()
        if not user:
            messagebox.showwarning("Input Error", "Username/Email cannot be empty!")
            return
        if acc_type == "tlauncher" and not pwd:
            result = messagebox.askyesno("Password Missing", "TLauncher account requires a password. Continue as offline?")
            if not result:
                return
        try:
            if account_manager.add_account(acc_type, user, pwd):
                self.refresh_account_list()
                self.username_entry.delete(0, tk.END)
                self.password_entry.delete(0, tk.END)
                self.set_status(f"Account '{user}' added/updated.", "green")
            else:
                self.set_status("Failed to add account.", "red")
        except Exception as e:
            messagebox.showerror("Account Error", f"Failed to add account: {e}")
            self.set_status(f"Error adding account: {e}", "red")
    
    def on_launch(self):
        selected_version = self.version_var.get()
        if not selected_version:
            messagebox.showerror("Error", "Please select a version.")
            return
        account_index = self.account_combo.current()
        if account_index == -1:
            accounts = account_manager.get_accounts()
            if not accounts:
                result = messagebox.askyesno("No Account", "Launch with default 'Player' username?")
                if result:
                    selected_account = {"type": "offline", "username": "Player", "uuid": str(uuidlib.uuid3(uuidlib.NAMESPACE_DNS, "Player")), "token": "0"}
                else:
                    return
            else:
                messagebox.showerror("Error", "Please select an account.")
                return
        else:
            selected_account = account_manager.get_account(account_index)
        ram_val = self.ram_var.get()
        java_path = self.java_entry.get().strip() or "java"
        server_ip = self.server_entry.get().strip() or None
        port_str = self.port_entry.get().strip()
        port = int(port_str) if port_str and port_str.isdigit() and 0 < int(port_str) < 65536 else None
        jvm_args_custom = self.jvm_args_entry.get().strip().split() if self.jvm_args_entry.get().strip() else None
        game_args_custom = self.game_args_entry.get().strip().split() if self.game_args_entry.get().strip() else None
        self.launch_btn.config(state="disabled")
        self.set_status("Starting launch process...", "blue")
        launch_thread = threading.Thread(target=self._launch_task, args=(selected_version, selected_account, ram_val, java_path, server_ip, port, jvm_args_custom, game_args_custom), daemon=True)
        launch_thread.start()
    
    def on_launch_server(self):
        server_version = self.server_version_var.get()
        if not server_version:
            messagebox.showerror("Error", "Please select a server version.")
            return
        ram_val = self.ram_var.get()
        java_path = self.java_entry.get().strip() or "java"
        self.launch_btn.config(state="disabled")
        self.set_status(f"Starting server for {server_version}...", "blue")
        server_thread = threading.Thread(target=self._launch_server_task, args=(server_version, ram_val, java_path), daemon=True)
        server_thread.start()
    
    def _launch_task(self, version_id, account, ram, java, server_ip, port, jvm_args_custom, game_args_custom):
        try:
            launch_game(version_id=version_id, account=account, ram_mb=ram, java_path=java, server_ip=server_ip, port=port, jvm_args_custom=jvm_args_custom, game_args_custom=game_args_custom, status_callback=self.set_status)
        except Exception as e:
            error_message = f"Error during launch: {e}"
            print(f"ERROR: {error_message}")
            import traceback
            traceback.print_exc()
            self.set_status(f"Error: {e}", "red")
            self.root.after(0, messagebox.showerror, "Launch Failed", error_message)
        finally:
            self.root.after(0, self.launch_btn.config, {"state": "normal"})
    
    def _launch_server_task(self, version_id, ram, java):
        try:
            launch_server(version_id=version_id, ram_mb=ram, java_path=java, status_callback=self.set_status)
        except Exception as e:
            error_message = f"Error launching server: {e}"
            print(f"ERROR: {error_message}")
            self.set_status(f"Error: {e}", "red")
            self.root.after(0, messagebox.showerror, "Server Launch Failed", error_message)
        finally:
            self.root.after(0, self.launch_btn.config, {"state": "normal"})

# --- Main Application ---
if __name__ == "__main__":
    root = tk.Tk()
    app = CatClientApp(root)
    root.mainloop()
