# Voice activity overlay

The green overlay stays hidden while recording and displays transcription, matching, planning, validation and execution stages afterward. Its percentage is a stage estimate, not measured model progress or a latency guarantee. Capture IDs and epochs reject stale progress updates.

Events coalesce on the gateway and travel over the persistent socket. Rendering runs on the device UI service, independently from the network worker. Authenticated `preview_loading_animation` is a labeled display demo and does not record audio or execute a skill.
