# Changelog

All notable changes to this project are documented in this file.

## 1.11.0 - 2026-10-23

### Features
- Model size column - every scanned model now shows its size before you commit to a download. Files on disk report the size declared in their safetensors header (correct even for a truncated download); missing models are size-probed in the background, shown with a leading `≈` because it is what the server reports rather than bytes you have
- "Use this version" now really switches the node - the alternatives dialog used to copy the filename to the clipboard and ask you to paste it somewhere. It now rewrites the model widget of every loader node using that file (subgraphs included), keeps the combo's option list in sync, updates the workflow's recorded model metadata and marks the workflow as modified
- Same model under a different filename is detected - ComfyUI's combo box matches the exact relative path, so `Qwen- image-2.1_int8_convrot.safetensors` (7,256,783,064 B) was invisible to a workflow asking for `qwen_image_2.1_int8_convrot.safetensors` (same 7,256,783,064 B): the node showed an empty field and the panel reported "missing". Such rows now carry a "same model, different name" chip with two one-click fixes - re-point the node, or rename the file in place (same folder, no copy, no download, refused unless both names normalize to the same model)

### Fixes
- Type badge no longer contradicts the save folder - the `^qwen.*(convrot|int8|bf16|...)` filename rule labelled a diffusion model and a VAE as "Text Encoder (Qwen)" while the folder column already said `diffusion_models` / `vae`. The loader node's own label now wins whenever it proves a different folder (`UNETLoader` -> Diffusion Model, `VAELoader` -> VAE)
- The folder button is back in the save-folder column for every row - it was gated on the file existing, so a workflow with nothing downloaded yet lost the button in the whole column. Missing rows now open the folder the model is supposed to land in
- Candidate sizes were always reported as 0 - `resolve-source` returned the size listed by the source API while the HEAD-probe result was stored in a different field
- Ghost "missing" rows after a re-point - the old filename survives inside the node's recorded download URL and got picked up by the scan again. A row is now dropped when no node references it any more and the same model is already on disk under another name

## 1.10.1 - 2026-10-21

### Improvements
- README rewritten in Chinese - documents the two fatal problems of the upstream release (models that could not be found, downloads that could not resume), lists every improvement of this fork with measured numbers, and states the ongoing-maintenance commitment
- Positioning is now explicit: a Chinese-optimized fork of the upstream node. UI, prompts and help docs are all in Chinese
- README gained a "network resilience" section documenting the ModelScope source architecture, the HF mirror priority chain, the aria2 flags and the real throughput-probe selection
- README settings table now lists all 7 native ComfyUI settings entries

### Fixes
- Fixed node/grid misalignment on high-DPI displays - `comfyui-mixlab-nodes` overrides `LGraphCanvas.drawBackCanvas` and detects the device pixel ratio by grepping the minified frontend source for `window.devicePixelRatio`; that literal no longer exists, so the background grid was permanently drawn at scale 1 while nodes rendered at the real DPR. A bundled web extension now keeps both canvases on the same ratio and follows live browser-zoom changes

### Compliance
- Removed runtime `pip install` - the Comfy Registry forbids installing packages via subprocess during node startup; missing dependencies are now only reported, with the install command shown
- `requires-python` corrected from `>=3.9` to `>=3.10` (the code uses 3.10+ syntax)
- Removed the leftover upstream `.circleci/config.yml` (an unused say-hello template)
- The publish workflow is now manual-dispatch only; pushing no longer publishes automatically

## 1.9.0 - 2026-10-21

### Features
- Automatic source resolution - missing models are resolved in the background and the best-verified download link is selected automatically
- Verified vs best-guess badge - every model shows whether its source is confirmed by objective checks or only a best guess, together with the evidence
- Evidence-based source scoring - candidates are ranked by remote size match, target-folder match, and official-publisher namespace instead of name guessing
- Persistent resolution cache - resolved sources survive restarts, so a re-scan no longer shows "no link"
- Correct target folder detection - models are matched and saved in the ComfyUI folder the loading node actually reads from
- Faster workflow scan - scanning is local-only, so the model table appears in well under a second

### Fixes
- Fixed a NameError in the download progress loop that aborted every download and forced endless retries
- Interrupted downloads now keep every byte already received instead of discarding the buffer, so a 10 GB model resumes instead of restarting
- Fixed model lookups for workflow references that carry a subfolder prefix (e.g. `moss\model.safetensors`)
- Removed duplicate "ghost" rows where the same model appeared twice, once with a wrong target folder
- Expected model size is now read from the safetensors header, so a half-downloaded file is still matched against the correct size
- Model lookups no longer pick a metadata-poor record and lose the repository/path information of the real match

### Documentation
- Removed internal development progress notes from the published repository
