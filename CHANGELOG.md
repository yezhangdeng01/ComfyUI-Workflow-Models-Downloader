# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Fixes
- Fixed node/grid misalignment on high-DPI displays - `comfyui-mixlab-nodes` overrides `LGraphCanvas.drawBackCanvas` and detects the device pixel ratio by grepping the minified frontend source for `window.devicePixelRatio`; that literal no longer exists, so the background grid was permanently drawn at scale 1 while nodes rendered at the real DPR. A bundled web extension now keeps both canvases on the same ratio and follows live browser-zoom changes

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
