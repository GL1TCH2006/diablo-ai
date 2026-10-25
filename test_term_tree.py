from tools.terminal import run_command
from voice.cancellation import CancellationToken
from config.settings import load_settings
import tempfile
from pathlib import Path
import threading
import time

settings = load_settings()
tmp = Path(tempfile.mkdtemp(prefix='term_tree_test_'))
print('Test dir:', tmp)

token = CancellationToken('test-term')
ctx = type('ctx', (), {'cancellation_token': token})()

# Command that spawns a child which prints its PID and sleeps
cmd = r'python -c "import subprocess, time, sys; p = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(30)\"]); print(\"Child PID:\", p.pid, flush=True); import time; [time.sleep(0.1) for _ in range(100)]"'

def delayed_cancel():
    time.sleep(0.5)
    token.request('cancel')

cancel_thread = threading.Thread(target=delayed_cancel, daemon=True)
cancel_thread.start()

result = run_command(cmd, working_dir=str(tmp), settings=settings, timeout=10, _tool_context=ctx)
cancel_thread.join(timeout=2)

print('Result success:', result.success)
print('Result cancelled:', result.cancelled)
print('Exit code:', result.exit_code)
print('Error:', result.error[:200] if result.error else 'None')

import shutil
shutil.rmtree(tmp, ignore_errors=True)
print('Test complete')