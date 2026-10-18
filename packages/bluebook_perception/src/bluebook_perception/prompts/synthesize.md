You are synthesizing supplementary knowledge for a hands-on task guide.

You will receive the full structured outline of a task: its steps, objects, safety notes, and transcript.

Your job is to produce:
1. prerequisites: things the learner must have or know before starting (tools, materials, access, prior skills). Derive from the video; do not invent.
2. faq: questions a complete novice would ask during or after watching. Cover the most common confusion points per step. Keep answers concise and accurate.
3. est_duration_s: realistic time for a novice to complete the task (not the expert's demo time). Base on step count and difficulty.

Rules:
- Only include FAQ answers you can support from the video or transcript.
- If a FAQ answer requires speculation, mark it clearly with "typically" or "usually".
- Deduplicate objects across steps (same object, same id).

Return valid JSON matching the schema exactly.
