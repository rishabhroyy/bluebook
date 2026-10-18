You are analysing an expert demonstration video of a hands-on task.

You will receive:
1. The full video (with audio narration).
2. A transcript of what the expert said, with timestamps.

Your job is to produce a structured outline of the task.

Rules:
- Steps must be ordered chronologically by t_start.
- Each step = one physical action (~10–60 s). Split anything longer.
- tooltip is imperative, ≤120 chars, names the specific object ("Turn the shutoff valve clockwise until it stops.").
- Objects must have a clear visual_description (colour, shape, size, typical location) so a camera can find them later. When two objects look alike (e.g. a battery and a memory card), say what tells them apart: colour, label, thickness, contacts, size relative to each other.
- For safety notes: only include what the expert explicitly said or demonstrated. Do NOT invent warnings.
- audio_cues: note any non-speech sounds that signal task events (clicks, hisses, motor changes, drips stopping).
- difficulty: beginner = common household task; intermediate = needs some skill/tools; advanced = specialist knowledge.

Return valid JSON matching the schema exactly.
