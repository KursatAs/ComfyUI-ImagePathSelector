import os
import sys
import hashlib
import sqlite3
import io as _io
try:
    import folder_paths as _folder_paths
except ImportError:
    _folder_paths = None
from PIL import Image, ImageDraw
import torch
import numpy as np

try:
    import rawpy
    _RAWPY_AVAILABLE = True
except ImportError:
    _RAWPY_AVAILABLE = False
    print("[ImagePathSelector] rawpy not found – RAW image support disabled. Install with: pip install rawpy")

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIF_AVAILABLE = True
except ImportError:
    _HEIF_AVAILABLE = False
    print("[ImagePathSelector] pillow-heif not found – HEIF/HIF support disabled. Install with: pip install pillow-heif")

from comfy_execution.graph import ExecutionBlocker


class ImagePathSelectorNode:
    _selection_storage = {}

    def __init__(self):
        self.image_cache = {}
        self.image_list = []
        self.directory_hash = None

    @classmethod
    def INPUT_TYPES(cls):
        if _folder_paths is not None:
            default_dir = _folder_paths.get_input_directory()
        else:
            default_dir = os.path.join(os.path.expanduser("~"), "Pictures")
            if not os.path.isdir(default_dir):
                default_dir = os.path.expanduser("~")
        return {
            "required": {
                "directory_path": ("STRING", {"default": default_dir, "multiline": False}),
                "refresh": ("BOOLEAN", {"default": False}),
                "selected_image": ("STRING", {"default": "", "forceInput": False}),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
                "prompt": "PROMPT"
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "select_image"
    CATEGORY = "image/selection"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, directory_path="", selected_image="", refresh=False, **kwargs):
        print(f"[IS_CHANGED] selected_image='{selected_image}' | refresh={refresh} | dir='{directory_path[:40]}'")
        if not selected_image:
            return float("nan")
        hash_input = f"{directory_path}|{selected_image}|{refresh}"
        stable_hash = hashlib.md5(hash_input.encode()).hexdigest()
        return stable_hash

    def _get_file_hash(self, filepath):
        hasher = hashlib.md5()
        hasher.update(filepath.encode('utf-8'))
        with open(filepath, 'rb') as f:
            hasher.update(f.read(1024))
        return hasher.hexdigest()

    def _open_image(self, image_path):
        ext = os.path.splitext(image_path.lower())[1]
        raw_extensions = {'.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2', '.raf', '.raw', '.pef', '.srw'}
        if _RAWPY_AVAILABLE and ext in raw_extensions:
            with rawpy.imread(image_path) as raw:
                rgb = raw.postprocess()
            return Image.fromarray(rgb)
        return Image.open(image_path)

    def _create_thumbnail(self, image_path, size):
        try:
            img = self._open_image(image_path)
            img = img.convert('RGB')
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            thumb_canvas = Image.new('RGB', (size, size), (32, 32, 32))
            x_offset = (size - img.width) // 2
            y_offset = (size - img.height) // 2
            thumb_canvas.paste(img, (x_offset, y_offset))
            return thumb_canvas
        except Exception as e:
            print(f"[ImagePathSelector] ✗ Thumbnail failed for '{os.path.basename(image_path)}': {e}")
            return None

    _DB_FILENAME = ".ImagePathSelector.db"

    def _get_db_path(self, directory_path):
        return os.path.join(directory_path, self._DB_FILENAME)

    def _set_hidden(self, filepath):

        if sys.platform == "win32":
            try:
                import ctypes
                FILE_ATTRIBUTE_HIDDEN = 0x2
                ctypes.windll.kernel32.SetFileAttributesW(filepath, FILE_ATTRIBUTE_HIDDEN)
            except Exception as e:
                print(f"[ImagePathSelector] ⚠ Could not set hidden attribute on '{filepath}': {e}")

    def _open_db(self, db_path):

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS thumbnails (
                hash       TEXT PRIMARY KEY,
                path       TEXT NOT NULL,
                thumbnail  BLOB NOT NULL,
                thumb_size INTEGER NOT NULL,
                sort_name  TEXT NOT NULL
            );
        """)
        conn.commit()
        return conn, cur

    def _read_meta(self, cur, key, default=None):
        row = cur.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _write_meta(self, cur, key, value):
        cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, str(value)))

    def _pil_to_blob(self, img):
        buf = _io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        return buf.getvalue()

    def _blob_to_pil(self, blob):
        return Image.open(_io.BytesIO(blob)).convert('RGB')

    def _write_thumb_to_db(self, cur, file_hash, filepath, thumb_pil, thumb_size):
        sort_name = os.path.basename(filepath).lower()
        blob = self._pil_to_blob(thumb_pil)
        cur.execute(
            "INSERT OR REPLACE INTO thumbnails (hash, path, thumbnail, thumb_size, sort_name) "
            "VALUES (?,?,?,?,?)",
            (file_hash, filepath, blob, thumb_size, sort_name)
        )

    def _load_from_db(self, db_path, thumb_size):

        try:
            conn, cur = self._open_db(db_path)
            with conn:
                stored_size = self._read_meta(cur, 'thumb_size')
                if stored_size is None or int(stored_size) != thumb_size:
                    print(f"[ImagePathSelector] ℹ Thumbnail size changed ({stored_size} → {thumb_size}), rebuilding .ImagePathSelector.db")
                    conn.close()
                    return None

                rows = cur.execute(
                    "SELECT hash, path, thumbnail FROM thumbnails ORDER BY sort_name"
                ).fetchall()
            conn.close()

            image_list = []
            for file_hash, filepath, blob in rows:
                if not os.path.isfile(filepath):
                    continue  # stale entry; will be cleaned on next refresh
                try:
                    thumb = self._blob_to_pil(blob)
                    self.image_cache[file_hash] = {'path': filepath, 'thumb': thumb}
                    image_list.append(file_hash)
                except Exception as e:
                    print(f"[ImagePathSelector] ⚠ Could not decode thumbnail for '{os.path.basename(filepath)}': {e}")
            return image_list
        except sqlite3.DatabaseError as e:
            print(f"[ImagePathSelector] ⚠ .ImagePathSelector.db is corrupt — rebuilding from scratch. ({e})")
            try:
                os.remove(db_path)
            except Exception:
                pass
            return None
        except PermissionError as e:
            print(f"[ImagePathSelector] ⚠ Cannot read .ImagePathSelector.db: Permission denied.")
            print(f"[ImagePathSelector]   → Thumbnails will not be cached. To enable caching, grant read access to that folder.")
            return None
        except Exception as e:
            print(f"[ImagePathSelector] ⚠ Unexpected error reading .ImagePathSelector.db: {e}")
            return None

    def _sync_db(self, db_path, directory_path, valid_extensions, thumb_size):

        try:
            conn, cur = self._open_db(db_path)

            stored_size = self._read_meta(cur, 'thumb_size')
            if stored_size is None or int(stored_size) != thumb_size:
                print(f"[ImagePathSelector] ℹ Thumbnail size changed — clearing .ImagePathSelector.db")
                cur.execute("DELETE FROM thumbnails")
                cur.execute("DELETE FROM meta")
                conn.commit()

            rows = cur.execute("SELECT hash, path FROM thumbnails").fetchall()
            db_hashes = {row[0]: row[1] for row in rows}

            fs_files = {}
            for filename in os.listdir(directory_path):
                if filename == self._DB_FILENAME:
                    continue
                ext = os.path.splitext(filename.lower())[1]
                if ext not in valid_extensions:
                    continue
                filepath = os.path.join(directory_path, filename)
                if not os.path.isfile(filepath):
                    continue
                file_hash = self._get_file_hash(filepath)
                fs_files[file_hash] = filepath

            stale = set(db_hashes.keys()) - set(fs_files.keys())
            if stale:
                cur.executemany("DELETE FROM thumbnails WHERE hash=?", [(h,) for h in stale])
                print(f"[ImagePathSelector] 🗑 Removed {len(stale)} stale entries from .ImagePathSelector.db")

            new_hashes = set(fs_files.keys()) - set(db_hashes.keys())
            added = 0
            for file_hash in new_hashes:
                filepath = fs_files[file_hash]
                thumb = self._create_thumbnail(filepath, thumb_size)
                if thumb:
                    self._write_thumb_to_db(cur, file_hash, filepath, thumb, thumb_size)
                    self.image_cache[file_hash] = {'path': filepath, 'thumb': thumb}
                    added += 1
            if added:
                print(f"[ImagePathSelector] ✚ Added {added} new thumbnail(s) to .ImagePathSelector.db")

            self._write_meta(cur, 'thumb_size', thumb_size)
            conn.commit()

            rows = cur.execute(
                "SELECT hash, path, thumbnail FROM thumbnails ORDER BY sort_name"
            ).fetchall()
            conn.close()

            image_list = []
            for file_hash, filepath, blob in rows:
                if file_hash not in self.image_cache:
                    try:
                        thumb = self._blob_to_pil(blob)
                        self.image_cache[file_hash] = {'path': filepath, 'thumb': thumb}
                    except Exception as e:
                        print(f"[ImagePathSelector] ⚠ Could not decode thumbnail for '{os.path.basename(filepath)}': {e}")
                        continue
                image_list.append(file_hash)
            return image_list

        except PermissionError:
            print(f"[ImagePathSelector] ⚠ Cannot write .ImagePathSelector.db in '{directory_path}': Permission denied.")
            print(f"[ImagePathSelector]   → Thumbnails will not be cached. To enable caching, grant write access to that folder.")
            return None
        except sqlite3.DatabaseError as e:
            print(f"[ImagePathSelector] ⚠ .ImagePathSelector.db is corrupt — rebuilding from scratch. ({e})")
            try:
                os.remove(db_path)
            except Exception:
                pass
            return None
        except Exception as e:
            print(f"[ImagePathSelector] ⚠ Unexpected error syncing .ImagePathSelector.db: {e}")
            return None

    def _load_images_from_directory(self, directory_path, thumbnail_size, refresh=False):
        if not os.path.exists(directory_path):
            return []

        valid_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp'}
        if _RAWPY_AVAILABLE:
            valid_extensions |= {'.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2', '.raf', '.raw', '.pef', '.srw'}
        if _HEIF_AVAILABLE:
            valid_extensions |= {'.hif', '.heic', '.heif'}

        db_path = self._get_db_path(directory_path)

        if not refresh and os.path.isfile(db_path):
            print(f"[ImagePathSelector] 📦 Loading thumbnails from .ImagePathSelector.db ...")
            cached = self._load_from_db(db_path, thumbnail_size)
            if cached is not None:
                print(f"[ImagePathSelector] ✓ Loaded {len(cached)} thumbnails from cache")
                return cached
            # DB was unusable — fall through to full regeneration

        if refresh and os.path.isfile(db_path):
            print(f"[ImagePathSelector] 🔄 Refreshing .ImagePathSelector.db ...")
            synced = self._sync_db(db_path, directory_path, valid_extensions, thumbnail_size)
            if synced is not None:
                print(f"[ImagePathSelector] ✓ Sync complete — {len(synced)} images in cache")
                return synced

        image_list = []
        all_files = os.listdir(directory_path)
        print(f"[ImagePathSelector] Scanning {len(all_files)} entries in directory...")

        found_exts = set(os.path.splitext(f.lower())[1] for f in all_files if os.path.isfile(os.path.join(directory_path, f)))
        print(f"[ImagePathSelector] Extensions found in directory: {sorted(found_exts)}")
        print(f"[ImagePathSelector] Valid extensions: {sorted(valid_extensions)}")

        db_conn = None
        db_cur = None
        db_available = False
        try:
            db_conn, db_cur = self._open_db(db_path)
            db_cur.execute("DELETE FROM thumbnails")  # fresh build
            db_cur.execute("DELETE FROM meta")
            db_available = True
            self._set_hidden(db_path)
        except PermissionError:
            print(f"[ImagePathSelector] ⚠ Cannot write .ImagePathSelector.db in '{directory_path}': Permission denied.")
            print(f"[ImagePathSelector]   → Thumbnails will not be cached. To enable caching, grant write access to that folder.")
        except Exception as e:
            print(f"[ImagePathSelector] ⚠ Unexpected error creating .ImagePathSelector.db: {e}")

        for filename in sorted(all_files):
            if filename == self._DB_FILENAME:
                continue
            ext = os.path.splitext(filename.lower())[1]
            if ext not in valid_extensions:
                continue
            filepath = os.path.join(directory_path, filename)
            if not os.path.isfile(filepath):
                continue

            file_hash = self._get_file_hash(filepath)

            if file_hash not in self.image_cache:
                thumb = self._create_thumbnail(filepath, thumbnail_size)
                if thumb:
                    self.image_cache[file_hash] = {'path': filepath, 'thumb': thumb}
                    if db_available:
                        self._write_thumb_to_db(db_cur, file_hash, filepath, thumb, thumbnail_size)
                else:
                    print(f"[ImagePathSelector]   ↳ Skipped (thumbnail creation failed): {filename}")
                    continue

            if file_hash in self.image_cache:
                image_list.append(file_hash)

        if db_available and db_conn:
            try:
                self._write_meta(db_cur, 'thumb_size', thumbnail_size)
                db_conn.commit()
                db_conn.close()
                print(f"[ImagePathSelector] 💾 Saved {len(image_list)} thumbnails to .ImagePathSelector.db")
            except Exception as e:
                print(f"[ImagePathSelector] ⚠ Could not finalise .ImagePathSelector.db: {e}")

        return image_list

    def _create_thumbnail_grid(self, image_hashes, cols=4):
        if not image_hashes:
            return torch.zeros((1, 64, 64, 3))

        thumbs = [self.image_cache[h]['thumb'] for h in image_hashes]
        rows = (len(thumbs) + cols - 1) // cols
        thumb_width = thumbs[0].width
        thumb_height = thumbs[0].height
        grid_width = cols * thumb_width
        grid_height = rows * thumb_height
        grid = Image.new('RGB', (grid_width, grid_height), (0, 0, 0))

        for idx, thumb in enumerate(thumbs):
            row = idx // cols
            col = idx % cols
            x = col * thumb_width
            y = row * thumb_height
            grid.paste(thumb, (x, y))

        grid_np = np.array(grid).astype(np.float32) / 255.0
        grid_tensor = torch.from_numpy(grid_np)[None,]
        return grid_tensor

    def _create_thumbnail_grid_with_selection(self, image_hashes, cols=4, selected_idx=0):
        if not image_hashes:
            return torch.zeros((1, 64, 64, 3))

        thumbs = [self.image_cache[h]['thumb'].copy() for h in image_hashes]
        rows = (len(thumbs) + cols - 1) // cols
        thumb_width = thumbs[0].width
        thumb_height = thumbs[0].height
        border = 4
        cell_width = thumb_width + border * 2
        cell_height = thumb_height + border * 2
        grid_width = cols * cell_width
        grid_height = rows * cell_height
        grid = Image.new('RGB', (grid_width, grid_height), (32, 32, 32))

        for idx, thumb in enumerate(thumbs):
            row = idx // cols
            col = idx % cols
            x = col * cell_width + border
            y = row * cell_height + border
            if idx == selected_idx:
                draw = ImageDraw.Draw(grid)
                border_coords = [
                    x - border, y - border,
                    x + thumb_width + border - 1, y + thumb_height + border - 1
                ]
                draw.rectangle(border_coords, outline=(0, 255, 0), width=border)
            grid.paste(thumb, (x, y))

        grid_np = np.array(grid).astype(np.float32) / 255.0
        return torch.from_numpy(grid_np)[None,]

    def _get_image_list_for_ui(self):
        import base64
        import io
        result = []
        for img_hash in self.image_list:
            cache_entry = self.image_cache.get(img_hash)
            if cache_entry:
                filepath = cache_entry['path']
                filename = os.path.basename(filepath)
                directory = os.path.dirname(filepath)
                thumb = cache_entry['thumb']
                buffer = io.BytesIO()
                thumb.save(buffer, format='JPEG', quality=85)
                buffer.seek(0)
                thumb_base64 = base64.b64encode(buffer.read()).decode('utf-8')
                thumb_b64_str = f"data:image/jpeg;base64,{thumb_base64}"
                result.append({
                    'path': filepath,
                    'filename': filename,
                    'directory': directory,
                    'hash': img_hash,
                    'thumbnail': thumb_b64_str
                })
        return result

    def _load_image_as_tensor(self, image_path):
        try:
            img = self._open_image(image_path)
            img = img.convert('RGB')
            img_np = np.array(img).astype(np.float32) / 255.0
            img_tensor = torch.from_numpy(img_np)[None,]
            return img_tensor
        except Exception as e:
            print(f"[ImagePathSelector] Error loading image {image_path}: {e}")
            return torch.zeros((1, 512, 512, 3))

    def _awaiting_storage(self, node_id):
        storage = self._selection_storage.setdefault(node_id, {})
        if "awaiting_selection" not in storage:
            storage["awaiting_selection"] = False
        return storage

    def _set_awaiting_state(self, node_id, awaiting):
        storage = self._awaiting_storage(node_id)
        storage["awaiting_selection"] = awaiting

    def _make_blocker_result(self):
        return tuple(ExecutionBlocker(None) for _ in self.RETURN_TYPES)

    def select_image(self, directory_path, refresh, selected_image="", unique_id=None, prompt=None):
        print(f"\n{'='*60}")
        print(f"[ImagePathSelector] ========== EXECUTING ==========")
        print(f"[ImagePathSelector] Directory: {directory_path}")
        print(f"[ImagePathSelector] Refresh: {refresh}")
        print(f"[ImagePathSelector] Selected image param TYPE: {type(selected_image)}")
        print(f"[ImagePathSelector] Selected image param LENGTH: {len(selected_image) if selected_image else 0}")
        print(f"[ImagePathSelector] Selected image param VALUE: '{selected_image}'")
        print(f"[ImagePathSelector] RAW support (rawpy):     {'✅ enabled' if _RAWPY_AVAILABLE else '❌ disabled – pip install rawpy'}")
        print(f"[ImagePathSelector] HEIF support (pillow-heif): {'✅ enabled' if _HEIF_AVAILABLE else '❌ disabled – pip install pillow-heif'}")

        thumbnail_size = 64
        columns = 8

        if not directory_path or not os.path.exists(directory_path):
            print(f"[ImagePathSelector] ✗ ERROR: Directory does not exist or is empty!")
            placeholder = torch.zeros((1, 512, 512, 3))
            empty_ui = {
                "images": [[]],
                "selected_index": [0],
                "thumbnail_size": [64],
                "columns": [8],
                "text": [""]
            }
            return {"ui": empty_ui, "result": (placeholder,)}

        dir_hash = hashlib.md5(directory_path.encode()).hexdigest()
        if self.directory_hash != dir_hash or refresh:
            print(f"[ImagePathSelector] Clearing in-memory cache (dir changed or refresh)")
            self.image_cache.clear()
            self.directory_hash = dir_hash

        self.image_list = self._load_images_from_directory(directory_path, thumbnail_size, refresh=refresh)
        print(f"[ImagePathSelector] ✓ Loaded {len(self.image_list)} images from directory")

        if not self.image_list:
            print(f"[ImagePathSelector] ✗ No valid images found in directory!")
            placeholder = torch.zeros((1, 512, 512, 3))
            return {"ui": {"images": [[]], "selected_index": [0], "thumbnail_size": [64], "columns": [8], "text": [""]}, "result": (placeholder,)}

        node_id = str(unique_id[0]) if isinstance(unique_id, list) else str(unique_id) if unique_id else "default"

        print(f"[ImagePathSelector] Preparing UI data with base64 thumbnails...")
        image_list = self._get_image_list_for_ui()
        print(f"[ImagePathSelector] ✓ Created {len(image_list)} thumbnail entries")

        if len(image_list) > 0:
            first_thumb_len = len(image_list[0].get('thumbnail', ''))
            print(f"[ImagePathSelector] First thumbnail base64 length: {first_thumb_len} chars")

        if selected_image and selected_image in [self.image_cache[h]['path'] for h in self.image_list]:
            selected_index = -1
            for idx, h in enumerate(self.image_list):
                if self.image_cache[h]['path'] == selected_image:
                    selected_index = idx
                    break

            print(f"[ImagePathSelector] ✓ User selected image at index {selected_index}")
            print(f"[ImagePathSelector] Selected path: {selected_image}")

            if node_id not in self._selection_storage:
                self._selection_storage[node_id] = {}
            self._selection_storage[node_id]["last_path"] = selected_image
            self._selection_storage[node_id]["last_index"] = selected_index
            self._set_awaiting_state(node_id, False)

            ui_data = {
                "images": [image_list],
                "selected_index": [selected_index],
                "thumbnail_size": [64],
                "columns": [8],
                "text": [selected_image],
                "awaiting_selection": [False]
            }

            print(f"[ImagePathSelector] Loading selected image as tensor...")
            image_tensor = self._load_image_as_tensor(selected_image)
            print(f"[ImagePathSelector] Image tensor shape: {image_tensor.shape}")
            print(f"[ImagePathSelector] ✓ Returning result with selected image")
            print(f"[ImagePathSelector] ========== COMPLETE ==========")
            print(f"{'='*60}\n")

            return {
                "ui": ui_data,
                "result": (image_tensor,)
            }
        else:
            print(f"[ImagePathSelector] ⏸️  PAUSED - No selection made yet")
            print(f"[ImagePathSelector] Displaying grid with {len(image_list)} thumbnails")
            print(f"[ImagePathSelector] Waiting for user to click a thumbnail...")
            print(f"[ImagePathSelector] Node ID: {node_id}")

            ui_data = {
                "images": [image_list],
                "selected_index": [-1],
                "thumbnail_size": [64],
                "columns": [8],
                "text": [""],
                "awaiting_selection": [True]
            }
            self._set_awaiting_state(node_id, True)

            print(f"[ImagePathSelector] Returning UI data (grid will be displayed)")
            print(f"[ImagePathSelector] When user clicks, node will re-execute with selection")
            print(f"[ImagePathSelector] ========== WAITING FOR USER INPUT ==========")
            print(f"{'='*60}\n")

            return {"ui": ui_data, "result": self._make_blocker_result()}


NODE_CLASS_MAPPINGS = {
    "ImagePathSelector": ImagePathSelectorNode
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ImagePathSelector": "Image Path Selector"
}

try:
    import sys
    from server import PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.get("/image_path_selector/is_windows")
    async def is_windows(request):
        return web.json_response({"windows": sys.platform == "win32"})

    if sys.platform == "win32":
        import threading
        import subprocess

        @PromptServer.instance.routes.get("/image_path_selector/browse_folder")
        async def browse_folder(request):
            initial_dir = request.rel_url.query.get("initial_dir", "")
            if not initial_dir or not os.path.isdir(initial_dir):
                initial_dir = os.path.expanduser("~")

            selected_path = {"result": None}

            def open_dialog():
                import tempfile
                ps_file = None
                try:
                    ps_code = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms

$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.Show()
$owner.Activate()

$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = "Navigate into your image folder and click Open"
$d.Filter = "All files (*.*)|*.*"
$d.FileName = "__select_folder__"
$d.ValidateNames = $false
$d.CheckFileExists = $false
$d.CheckPathExists = $false
$d.Multiselect = $false
$d.InitialDirectory = $args[0]
if ($d.ShowDialog($owner) -eq "OK") {
    $raw = $d.FileName
    if ([System.IO.Directory]::Exists($raw)) {
        Write-Output $raw
    } else {
        $dir = [System.IO.Path]::GetDirectoryName($raw)
        if ($dir -and [System.IO.Directory]::Exists($dir)) {
            Write-Output $dir
        }
    }
}
$owner.Dispose()
"""
                    fd, ps_file = tempfile.mkstemp(suffix=".ps1")
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        f.write(ps_code)

                    result = subprocess.run(
                        ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                         "-ExecutionPolicy", "Bypass", "-File", ps_file, initial_dir],
                        capture_output=True, text=True, encoding='utf-8', timeout=120
                    )
                    path = result.stdout.strip()
                    if result.stderr.strip():
                        print(f"[ImagePathSelector] PowerShell stderr: {result.stderr.strip()}")
                    selected_path["result"] = path if path else None
                except Exception as e:
                    print(f"[ImagePathSelector] Folder dialog error: {e}")
                    selected_path["result"] = None
                finally:
                    if ps_file and os.path.exists(ps_file):
                        try:
                            os.remove(ps_file)
                        except Exception:
                            pass

            t = threading.Thread(target=open_dialog)
            t.start()
            t.join(timeout=120)

            return web.json_response({"path": selected_path["result"]})

except Exception as e:
    print(f"[ImagePathSelector] Could not register API routes: {e}")
