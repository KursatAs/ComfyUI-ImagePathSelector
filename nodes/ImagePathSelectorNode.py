import os
import hashlib
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

    def _load_images_from_directory(self, directory_path, thumbnail_size):
        if not os.path.exists(directory_path):
            return []

        valid_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp'}
        if _RAWPY_AVAILABLE:
            valid_extensions |= {'.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2', '.raf', '.raw', '.pef', '.srw'}
        if _HEIF_AVAILABLE:
            valid_extensions |= {'.hif', '.heic', '.heif'}
        image_list = []

        all_files = os.listdir(directory_path)
        print(f"[ImagePathSelector] Scanning {len(all_files)} entries in directory...")

        found_exts = set(os.path.splitext(f.lower())[1] for f in all_files if os.path.isfile(os.path.join(directory_path, f)))
        print(f"[ImagePathSelector] Extensions found in directory: {sorted(found_exts)}")
        print(f"[ImagePathSelector] Valid extensions: {sorted(valid_extensions)}")

        for filename in all_files:
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
                    self.image_cache[file_hash] = {
                        'path': filepath,
                        'thumb': thumb
                    }
                else:
                    print(f"[ImagePathSelector]   ↳ Skipped (thumbnail creation failed): {filename}")
                    continue

            if file_hash in self.image_cache:
                image_list.append(file_hash)

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
            print(f"[ImagePathSelector] Clearing cache (dir changed or refresh)")
            self.image_cache.clear()
            self.directory_hash = dir_hash

        self.image_list = self._load_images_from_directory(directory_path, thumbnail_size)
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
