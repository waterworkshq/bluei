#!/usr/bin/env node
/**
 * QA Agent — Node.js entry wrapper
 *
 * Responsibilities:
 *  - Detect / bootstrap Python environment on first run
 *  - Pass all arguments through to the bash runner
 *  - Handle the --version flag directly (no Python needed)
 */

const { execSync, spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const WRAPPER_VERSION = '2.5.0';

// Resolve the qa-agent workspace (one level up from bin/)
const WRAPPER_DIR = path.resolve(__dirname, '..');
const ROOT_DIR = WRAPPER_DIR;
const VENV_PYTHON = path.join(WRAPPER_DIR, '.venv', 'bin', 'python3');
const BASH_SCRIPT = path.join(WRAPPER_DIR, 'qa-agent');
const BOOTSTRAP = path.join(WRAPPER_DIR, 'scripts', 'bootstrap.sh');

function log(msg) {
  process.stderr.write(msg + '\n');
}

function which(cmd) {
  try {
    execSync(`which ${cmd}`, { stdio: 'pipe' });
    return true;
  } catch (_) {
    return false;
  }
}

function runPython(cmd, extraEnv) {
  const env = { ...process.env, ...extraEnv };
  return execSync(cmd, {
    cwd: WRAPPER_DIR,
    env,
    stdio: 'inherit',
  });
}

function hasVenv() {
  return fs.existsSync(VENV_PYTHON);
}

function pythonWorks() {
  try {
    execSync(`${VENV_PYTHON} --version`, { stdio: 'pipe' });
    return true;
  } catch (_) {
    return false;
  }
}

function bootstrap() {
  log('qa-agent: Python environment not found, running bootstrap...');
  try {
    execSync(`bash "${BOOTSTRAP}"`, { cwd: WRAPPER_DIR, stdio: 'inherit' });
    return true;
  } catch (e) {
    return false;
  }
}

function main() {
  const args = process.argv.slice(2);

  // Built-in flags that don't need Python
  if (args.includes('--version') || args.includes('-v')) {
    // Try to get Python version first, fall back to wrapper version
    if (which('python3')) {
      try {
        const v = execSync('python3 -c "import sys; print(sys.executable)"', { stdio: 'pipe' }).toString().trim();
      } catch (_) {}
    }
    console.log(`qa-agent ${WRAPPER_VERSION}`);
    return 0;
  }

  if (args.includes('--help') || args.includes('-h')) {
    // help works without Python — use python3 explicitly
    const pyBin = hasVenv()
      ? path.join(WRAPPER_DIR, '.venv', 'bin', 'python3')
      : (which('python3') ? 'python3' : 'python');
    if (fs.existsSync(BASH_SCRIPT)) {
      try {
        execSync(`${pyBin} "${BASH_SCRIPT}" --help`, { cwd: WRAPPER_DIR, stdio: 'inherit' });
        return 0;
      } catch (e) {
        // fall through
      }
    }
  }

  // Ensure Python env is ready before any command that needs it
  const needsPython = !args.some(a => ['--version', '-v', '--help', '-h', 'init'].includes(a));

  if (needsPython || args.includes('init')) {
    if (!hasVenv() || !pythonWorks()) {
      if (!bootstrap()) {
        log('qa-agent: bootstrap failed. Run bash scripts/bootstrap.sh manually.');
        return 1;
      }
    }
  }

  // Determine Python interpreter
  const pythonBin = hasVenv()
    ? path.join(WRAPPER_DIR, '.venv', 'bin', 'python3')
    : (which('python3') ? 'python3' : 'python');

  try {
    const result = spawn(pythonBin, [BASH_SCRIPT, ...args], {
      cwd: WRAPPER_DIR,
      stdio: 'inherit',
      env: { ...process.env }
    });
    result.on('close', code => process.exit(code || 0));
    return 0; // exit handled by the spawn above
  } catch (e) {
    log(`qa-agent: failed to run: ${e.message}`);
    return e.status || 1;
  }
}

process.exit(main());
