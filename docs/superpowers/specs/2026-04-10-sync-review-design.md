# Sync & Review Feature Design Spec

**Date:** 2026-04-10
**Status:** Approved

## Summary

Add Synchronization trigger + Episode Review to the SyncField web viewer. Completely separate from the Recording view, accessible via header segment control.

## Decisions

| Item | Decision |
|------|----------|
| Navigation | Header inline segment control (Record \| Review) |
| Review first page | Card Grid + Table view toggle |
| Episode detail | Video + Right Sidebar layout |
| Sync backend | Local Docker container (localhost:8080) default |
| Review depth | Standard — videos, sync quality, before/after drift chart |
| Drift chart | frame_map.jsonl based, gray dashed (before) + green solid (after) |

## Architecture

### Backend API (server.py additions)

```
GET  /api/episodes                    → episode list from data root
GET  /api/episodes/{id}               → manifest + sync_report
GET  /api/episodes/{id}/video/{stream} → serve video file
POST /api/episodes/{id}/sync          → proxy to Docker sync API
GET  /api/episodes/{id}/sync-status/{job_id} → poll proxy
GET  /api/episodes/{id}/frame-map     → parsed frame_map.jsonl
```

### Frontend Components

```
App
├── SegmentControl (Record | Review)
├── [Record mode] — existing recording UI
└── [Review mode]
    ├── EpisodeList
    │   ├── ViewToggle (Grid | Table)
    │   ├── EpisodeGrid → EpisodeCard[]
    │   └── EpisodeTable
    └── EpisodeDetail (on card click)
        ├── Header (← back, episode name, sync button)
        ├── VideoPlayer[] (multi-camera sync)
        ├── Timeline (scrubber + playback controls)
        ├── DriftChart (before/after)
        └── SyncSidebar
            ├── SyncQualityPanel (grade, confidence, improvement)
            ├── StreamList (per-stream offset)
            └── Metadata (duration, fps, host)
```

### Sync Flow

1. User clicks Sync button
2. Server reads episode manifest, builds sync request
3. POST to Docker container `/api/v1/sync` with local paths
4. Poll `/api/v1/jobs/{job_id}` every 3s
5. On complete: read sync_report.json, switch to synced/ videos
6. Default endpoint: `http://localhost:8080`, configurable via launch param
