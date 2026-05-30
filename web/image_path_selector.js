import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

const unwrapValue = (value, fallback) => {
    if (Array.isArray(value)) {
        return value.length ? value[0] : fallback;
    }
    return value !== undefined ? value : fallback;
};

const normalizeImageList = (images) => {
    if (!Array.isArray(images)) {
        return null;
    }
    if (images.length === 1 && Array.isArray(images[0])) {
        return images[0];
    }
    return images;
};

const extractUiPayload = (message) => {
    const candidates = [];
    const addCandidate = (obj, source) => {
        if (obj && typeof obj === "object") {
            candidates.push({ data: obj, source });
        }
    };

    addCandidate(message, "message");
    addCandidate(message?.output, "message.output");
    addCandidate(message?.ui, "message.ui");
    addCandidate(message?.output?.ui, "message.output.ui");

    for (const candidate of candidates) {
        const imageList = normalizeImageList(candidate.data?.images);
        if (imageList) {
            return {
                source: candidate.source,
                imageList,
                selectedIndex: unwrapValue(candidate.data?.selected_index, -1),
                thumbnailSize: unwrapValue(candidate.data?.thumbnail_size, 64),
                columns: unwrapValue(candidate.data?.columns, 8)
            };
        }
    }
    return null;
};

console.log("[ImagePathSelector] Extension loading...");

let _isWindows = false;

app.registerExtension({
    name: "Comfy.ImagePathSelector",

    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        console.log("[ImagePathSelector] beforeRegisterNodeDef called for:", nodeData.name);
        if (nodeData.name === "ImagePathSelector") {
            const onNodeCreated = nodeType.prototype.onNodeCreated;

            nodeType.prototype.onNodeCreated = function() {
                const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;

                this.imageListData = [];
                this.thumbnailSize = 64;
                this.columns = 8;
                this.selectedImagePath = "";
                this.selectedIndex = -1;
                this.lastImageCount = 0;

                const self = this;

                const gridContainer = document.createElement("div");
                gridContainer.style.cssText =
                    "width:100%;min-height:80px;background:#202020;overflow:auto;" +
                    "box-sizing:border-box;cursor:pointer;border-radius:4px;";

                const gridCanvas = document.createElement("canvas");
                gridCanvas.style.cssText = "display:block;";
                gridContainer.appendChild(gridCanvas);

                this._gridCanvas = gridCanvas;
                this._gridContainer = gridContainer;
                this._gridWidgetHeight = 80;

                gridCanvas.addEventListener("click", async (e) => {
                    if (!self.imageListData || self.imageListData.length === 0) return;

                    const rect = gridCanvas.getBoundingClientRect();
                    const scaleX = gridCanvas.width  / rect.width;
                    const scaleY = gridCanvas.height / rect.height;
                    const relX = (e.clientX - rect.left) * scaleX;
                    const relY = (e.clientY - rect.top)  * scaleY;

                    const border = 4;
                    const cellWidth  = self.thumbnailSize + border * 2;
                    const cellHeight = self.thumbnailSize + border * 2;

                    const col = Math.floor(relX / cellWidth);
                    const row = Math.floor(relY / cellHeight);
                    const clickedIndex = row * self.columns + col;

                    if (clickedIndex < 0 || clickedIndex >= self.imageListData.length) return;

                    self.selectedIndex = clickedIndex;
                    const selectedImage = self.imageListData[clickedIndex];
                    self.selectedImagePath = selectedImage.path;

                    console.log(`[ImagePathSelector] ===== IMAGE CLICKED =====`);
                    console.log(`[ImagePathSelector] Index: ${clickedIndex} | File: ${selectedImage.filename}`);
                    console.log(`[ImagePathSelector] Path: ${selectedImage.path}`);

                    const hiddenWidget = self.widgets.find(w => w.name === "selected_image");
                    if (hiddenWidget) {
                        hiddenWidget.value = selectedImage.path;
                        if (!hiddenWidget.serialize_value) {
                            hiddenWidget.serialize_value = function() { return this.value; };
                        }
                        if (hiddenWidget.callback) hiddenWidget.callback(hiddenWidget.value);
                    } else {
                        console.error("[ImagePathSelector] Hidden widget 'selected_image' not found!");
                    }

                    await self.buildThumbnailGrid();
                    app.queuePrompt(0, 1);
                });

                this.applyPayloadToGrid = async function(payload, sourceLabel = "payload") {
                    if (!payload) {
                        console.log(`[ImagePathSelector] ✗ No payload (${sourceLabel})`);
                        self.imageListData = [];
                        self.selectedIndex = -1;
                        self.lastImageCount = 0;
                        self._clearGrid();
                        return;
                    }

                    console.log(`[ImagePathSelector] Applying payload from ${sourceLabel}`);
                    self.imageListData  = Array.isArray(payload.imageList) ? payload.imageList : [];
                    self.selectedIndex  = Number.isInteger(payload.selectedIndex) ? payload.selectedIndex : -1;
                    self.thumbnailSize  = payload.thumbnailSize ?? self.thumbnailSize;
                    self.columns        = payload.columns        ?? self.columns;

                    console.log(`[ImagePathSelector] ✓ ${self.imageListData.length} images, sel=${self.selectedIndex}`);

                    if (!self.imageListData.length) {
                        self.selectedIndex  = -1;
                        self.lastImageCount = 0;
                        self._clearGrid();
                        return;
                    }

                    await self.buildThumbnailGrid();
                    self.lastImageCount = self.imageListData.length;
                    console.log("[ImagePathSelector] ✓ Grid built");
                };

                this._clearGrid = function() {
                    const gc = self._gridCanvas;
                    if (!gc) return;
                    gc.width  = 1;
                    gc.height = 1;
                    const ctx = gc.getContext("2d");
                    ctx.clearRect(0, 0, 1, 1);
                    self._gridWidgetHeight = 0;
                    self._gridContainer.style.height    = "0px";
                    self._gridContainer.style.minHeight = "0px";
                    self._gridContainer.style.setProperty("--comfy-widget-height",     "0px");
                    self._gridContainer.style.setProperty("--comfy-widget-min-height", "0px");
                    if (self._domWidget) self._domWidget.computedHeight = 0;
                    self._forceResize();
                };

                this._forceResize = function() {
                    const gc      = self._gridCanvas;
                    const targetH = self._gridWidgetHeight;

                    if (self._domWidget) self._domWidget.computedHeight = targetH;

                    self._gridContainer.style.setProperty("--comfy-widget-height",     targetH + "px");
                    self._gridContainer.style.setProperty("--comfy-widget-min-height", targetH + "px");

                    self._gridContainer.style.height    = targetH + "px";
                    self._gridContainer.style.minHeight = targetH + "px";

                    if (typeof self.setSize === "function") {
                        const newSize = self.computeSize();
                        if (gc) newSize[0] = Math.max(newSize[0], gc.width + 40);
                        self.setSize(newSize);
                    }
                    if (typeof self.setDirtyCanvas === "function") self.setDirtyCanvas(true, true);
                    if (app.graph?.setDirtyCanvas) app.graph.setDirtyCanvas(true, true);
                };

                this.hasLoadedOnce = false;
                const originalOnExecuted = this.onExecuted;
                this.onExecuted = async function(message) {
                    if (originalOnExecuted) originalOnExecuted.apply(this, arguments);

                    console.log("[ImagePathSelector] ===== onExecuted START =====");
                    console.log("[ImagePathSelector] Message keys:", message ? Object.keys(message) : "null");

                    const payload = extractUiPayload(message);
                    if (payload) {
                        await self.applyPayloadToGrid(payload, `onExecuted:${payload.source}`);
                    } else {
                        console.log("[ImagePathSelector] ✗ No valid UI payload");
                        await self.applyPayloadToGrid(null, "onExecuted:none");
                    }
                    console.log("[ImagePathSelector] ===== onExecuted END =====");
                };

                this.buildThumbnailGrid = async function() {
                    const gc = self._gridCanvas;
                    if (!gc) return;

                    if (!self.imageListData || self.imageListData.length === 0) {
                        self._clearGrid();
                        return;
                    }

                    console.log(`[ImagePathSelector] Building grid: ${self.imageListData.length} images`);

                    const border     = 4;
                    const cellWidth  = self.thumbnailSize + border * 2;
                    const cellHeight = self.thumbnailSize + border * 2;
                    const rows       = Math.ceil(self.imageListData.length / self.columns);

                    gc.width  = self.columns * cellWidth;
                    gc.height = rows       * cellHeight;

                    self._gridWidgetHeight = gc.height;
                    if (self._domWidget) self._domWidget.computedHeight = gc.height;
                    self._gridContainer.style.setProperty("--comfy-widget-height",     gc.height + "px");
                    self._gridContainer.style.setProperty("--comfy-widget-min-height", gc.height + "px");
                    self._gridContainer.style.height    = gc.height + "px";
                    self._gridContainer.style.minHeight = gc.height + "px";

                    const ctx = gc.getContext("2d");
                    ctx.fillStyle = "#202020";
                    ctx.fillRect(0, 0, gc.width, gc.height);

                    const loadPromises = self.imageListData.map((imgData, idx) => {
                        const row = Math.floor(idx / self.columns);
                        const col = idx % self.columns;
                        const x   = col * cellWidth;
                        const y   = row * cellHeight;

                        if (self.selectedIndex >= 0 && idx === self.selectedIndex) {
                            ctx.strokeStyle = "#00ff00";
                            ctx.lineWidth   = border;
                            ctx.strokeRect(x, y, cellWidth, cellHeight);
                        }

                        return new Promise((resolve) => {
                            const img = new Image();
                            img.src = imgData.thumbnail;

                            img.onload = () => {
                                const ar = img.width / img.height;
                                let dw = self.thumbnailSize, dh = self.thumbnailSize;
                                let ox = 0, oy = 0;
                                if (ar > 1) { dh = self.thumbnailSize / ar; oy = (self.thumbnailSize - dh) / 2; }
                                else if (ar < 1) { dw = self.thumbnailSize * ar; ox = (self.thumbnailSize - dw) / 2; }

                                ctx.fillStyle = "#202020";
                                ctx.fillRect(x + border, y + border, self.thumbnailSize, self.thumbnailSize);
                                ctx.drawImage(img, x + border + ox, y + border + oy, dw, dh);
                                resolve();
                            };

                            img.onerror = () => {
                                ctx.fillStyle = "#404040";
                                ctx.fillRect(x + border, y + border, self.thumbnailSize, self.thumbnailSize);
                                ctx.fillStyle = "#ffffff";
                                ctx.font = "12px Arial";
                                ctx.fillText("Error", x + border + 4, y + border + self.thumbnailSize / 2);
                                resolve();
                            };
                        });
                    });

                    await Promise.all(loadPromises);

                    self._forceResize();
                    requestAnimationFrame(() => self._forceResize());
                    console.log("[ImagePathSelector] Grid built successfully");
                };

                const originalSerialize = this.serialize;
                this.serialize = function() {
                    const data = originalSerialize ? originalSerialize.apply(this, arguments) : {};
                    const hiddenWidget = this.widgets?.find(w => w.name === "selected_image");
                    if (hiddenWidget && hiddenWidget.value) {
                        if (!data.widgets_values) data.widgets_values = [];
                        const idx = this.widgets.indexOf(hiddenWidget);
                        if (idx >= 0) data.widgets_values[idx] = hiddenWidget.value;
                    }
                    return data;
                };

                this.getExtraMenuOptions = function(canvas, options) {
                    options.unshift({
                        content: "Reload Images",
                        callback: () => {
                            const refreshWidget = self.widgets.find(w => w.name === "refresh");
                            if (refreshWidget) refreshWidget.value = !refreshWidget.value;
                            app.queuePrompt(0, 1);
                        }
                    });
                };

                this.widgets = this.widgets.filter(
                    w => w.name !== "thumbnail_size" && w.name !== "columns"
                );

                if (_isWindows) {
                    const browseBtn = this.addWidget("button", "📁 Browse Folder...", "browse_folder_btn", async () => {
                        const dirWidget = self.widgets.find(w => w.name === "directory_path");
                        const currentDir = dirWidget?.value ?? "";
                        browseBtn.name = "⏳ Waiting for selection...";
                        if (typeof self.setDirtyCanvas === "function") self.setDirtyCanvas(true, true);
                        try {
                            const url = "/image_path_selector/browse_folder" +
                                (currentDir ? "?initial_dir=" + encodeURIComponent(currentDir) : "");
                            const response = await fetch(url);
                            if (!response.ok) throw new Error("HTTP " + response.status);
                            const data = await response.json();
                            if (data.path) {
                                if (dirWidget) {
                                    dirWidget.value = data.path;
                                    if (dirWidget.callback) dirWidget.callback(data.path);
                                }
                                console.log("[ImagePathSelector] Folder selected:", data.path);
                            }
                        } catch (e) {
                            console.error("[ImagePathSelector] Browse folder error:", e);
                        } finally {
                            browseBtn.name = "📁 Browse Folder...";
                            if (typeof self.setDirtyCanvas === "function") self.setDirtyCanvas(true, true);
                        }
                    });
                }

                this._domWidget = this.addDOMWidget("thumbnail_grid_widget", "thumbnail_grid", gridContainer, {
                    serialize: false,
                    hideOnZoom: false,
                    getHeight:    () => self._gridWidgetHeight,
                    getMinHeight: () => self._gridWidgetHeight,
                    getMaxHeight: () => self._gridWidgetHeight,
                });
                if (this._domWidget) {
                    this._domWidget.computedHeight = this._gridWidgetHeight;
                }

                return r;
            };

            const onExecutionStart = nodeType.prototype.onExecutionStart;
            nodeType.prototype.onExecutionStart = function() {
                if (onExecutionStart) onExecutionStart.apply(this, arguments);
                console.log("[ImagePathSelector] Execution started");
            };
        }
    },

    async setup() {
        console.log("[ImagePathSelector] Setup function called");

        try {
            const resp = await fetch("/image_path_selector/is_windows");
            if (resp.ok) {
                const data = await resp.json();
                _isWindows = !!data.windows;
            }
        } catch (e) {
            console.warn("[ImagePathSelector] Could not determine platform, Browse button disabled:", e);
        }
        console.log(`[ImagePathSelector] Windows host: ${_isWindows}`);

        const api = app.api;

        api.addEventListener("executed", async (event) => {
            console.log("[ImagePathSelector] ===== API EVENT RECEIVED =====");
            const data = event.detail;

            if (data?.node) {
                const node = app.graph.getNodeById(data.node);
                if (node && node.type === "ImagePathSelector") {
                    const payload = extractUiPayload(data);
                    if (payload) {
                        await node.applyPayloadToGrid(payload, `api:${payload.source}`);
                    }
                }
            }
            console.log("[ImagePathSelector] ===== API EVENT END =====");
        });

        console.log("[ImagePathSelector] Setup complete");
    }
});
