You are filling in the details of ONE step from a hands-on task demonstration.

You will receive:
1. A short clip of the video covering this step (with audio).
2. The transcript slice for this step.
3. The step's title and approximate time range.
4. The full list of objects already identified for this task.

Your job is to fill every field of the step completely and accurately.

Rules:
- tooltip: imperative, ≤120 chars, names the specific object being acted on. ("Turn the shutoff valve clockwise until it stops.")
- instruction: full step-by-step detail a novice can follow.
- success_criteria: list of things observable by a camera ("Handle is perpendicular to the pipe", "No water drips from the fitting"). These must be camera-verifiable.
- objects: list each object used/acted on in this step with its role. The thing the user acts ON is "target". Tools used are "tool". Materials consumed are "material". Things nearby for context are "context".
- narration: copy the exact words from the transcript that cover this step.
- keyframe_times: suggest 1–3 timestamps (in seconds from video start) where a clear, sharp view of the key action would make a good reference image.
- NEVER invent torque values, measurements, part numbers, or safety specs not in the video or narration. If unsure, omit.

Return valid JSON matching the schema exactly.
