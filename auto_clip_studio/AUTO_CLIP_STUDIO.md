# Chopster — Auto Clip Studio

Auto Clip Studio is Chopster's integrated workspace for analyzing long-form video, selecting moments, adjusting clips, styling captions, and rendering short-form exports.

## Run inside Chopster

Start the desktop application with `run_chopster.bat` on Windows. Open **Auto Clip Studio** from the Chopster workspace navigation; the local frontend and API start together and remain embedded in the desktop application.

## Development

The React and TypeScript source is in `web_source/`. The ready-to-use production bundle is in `web_dist/`. Rebuild the frontend after changing React, TypeScript, or CSS:

```text
cd chopster/auto_clip_studio/web_source
npm ci
npm run lint
npm run build
```

Auto Clip Studio uses its own Gemini key, browser profile, local history, temporary media, and export directory. Its settings are separate from Content Clipper AI.

## Video and data handling

The local API binds to loopback only. YouTube cookies and Gemini credentials are kept in the local application profile. Temporary media can be cleared from the workspace; export files are stored separately.