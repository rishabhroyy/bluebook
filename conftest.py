"""Tests never touch the laptop's real ./data (Part 3 writes calib/diag/server.log there when BB_INTERACTIVE=1).
Real env vars beat ./.env, so this runs before any bluebook_core import and wins."""

import os
import tempfile

os.environ["BB_DATA_DIR"] = tempfile.mkdtemp(prefix="bluebook-test-data-")
