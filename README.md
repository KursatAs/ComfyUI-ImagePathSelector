# ComfyUI-ImagePathSelector

A custom ComfyUI node that lets you visually browse a folder of images and select one to feed into your workflow — directly from the node's thumbnail grid.

---
![node.png](images/node.png)
## Features

- 🖼️ **Visual thumbnail grid** — displays all images in a chosen directory as a scrollable grid inside the node
- 🖱️ **Click to select** — click any thumbnail to instantly queue and run the workflow with that image
- 📁 **Browse Folder button** *(Windows only)* — opens a native folder picker dialog
- ✅ **Green border highlight** on the currently selected image
- 🔄 **Refresh toggle** — force-reload the image list from disk
- 🗂️ **Right-click → Reload Images** context menu option
- 💾 **Selection persists** across workflow saves and reloads

---

## Supported Formats

| Format | Notes |
|---|---|
| JPEG, PNG, BMP, GIF, WebP | Always supported |
| HEIC / HEIF / HIF | Requires `pillow-heif` |
| CR2, CR3, NEF, ARW, DNG, ORF, RW2, RAF, RAW, PEF, SRW | Requires `rawpy` |

---

## Installation

### Via ComfyUI Manager *(recommended)*
Search for **ComfyUI-ImagePathSelector** in the ComfyUI Manager and install.

### Manual
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/KursatAs/ComfyUI-ImagePathSelector
```

### Optional dependencies
For RAW camera format support:
```bash
pip install rawpy
```
For HEIC/HEIF support:
```bash
pip install pillow-heif
```

---

## Usage

1. Add the **Image Path Selector** node to your workflow (`image/selection` category).
2. Set the **`directory_path`** to the folder containing your images.
3. Run the workflow — the node will scan the folder and display a thumbnail grid.
4. **Click a thumbnail** to select it. The workflow will automatically re-queue and pass the selected image downstream.
5. The selected image path is shown below the grid. The selected thumbnail is highlighted with a **green border**.

### Inputs

| Name | Type | Description |
|---|---|---|
| `directory_path` | STRING | Absolute path to the image folder |
| `refresh` | BOOLEAN | Toggle to force a rescan of the directory |
| `selected_image` | STRING | Path of the currently selected image (set automatically on click) |

### Outputs

| Name | Type | Description |
|---|---|---|
| `image` | IMAGE | The selected image as a tensor, ready for use in your workflow |

> **Note:** If no image has been selected yet, the node will block workflow execution and wait for the user to click a thumbnail.

---

## Tips

- On **Windows**, use the **📁 Browse Folder...** button to open a native folder picker instead of typing the path manually.
- Use **right-click → Reload Images** or toggle the **Refresh** checkbox to pick up newly added files without restarting ComfyUI.
- The thumbnail cache is automatically invalidated when the directory path changes or refresh is triggered.
- The Image Path Selector node creates thumbnails while loading images, so the loading time may vary depending on the number, size, and type of images in the selected folder. 
- RAW image formats load more slowly because they usually have larger file sizes. It is not recommended to load a folder containing more than approximately 50–100 images.


---

## Requirements

- ComfyUI
- Python packages: `Pillow`, `torch`, `numpy`
- *(Optional)* `rawpy` — for RAW camera formats
- *(Optional)* `pillow-heif` — for HEIC/HEIF formats

---

## License

See [LICENSE](LICENSE).

---

## Links

- 🐛 [Bug Tracker](https://github.com/KursatAs/ComfyUI-ImagePathSelector/issues)
- 📖 [Documentation / Wiki](https://github.com/KursatAs/ComfyUI-ImagePathSelector/wiki)
- 📦 [Comfy Registry](https://registry.comfy.org/nodes/ComfyUI-ImagePathSelector)

