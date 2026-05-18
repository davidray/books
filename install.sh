#!/usr/bin/env bash
# install.sh — Set up dabble-mcp with a Dabble export file.
set -euo pipefail

BOLD='\033[1m'
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'

step()  { echo -e "\n${CYAN}▶ $*${RESET}"; }
ok()    { echo -e "${GREEN}✓ $*${RESET}"; }
warn()  { echo -e "${YELLOW}⚠ $*${RESET}"; }
error() { echo -e "${RED}✗ $*${RESET}" >&2; exit 1; }

echo -e "\n${BOLD}dabble-mcp setup${RESET}"
echo "────────────────────────────────────────"

# ── 1. Find dabble-mcp ────────────────────────────────────────────────────────
step "Checking for dabble-mcp..."
if ! command -v dabble-mcp &>/dev/null; then
    error "dabble-mcp not found. Install it first:\n  python3.11 -m pip install -e ."
fi
ok "dabble-mcp found at $(command -v dabble-mcp)"

# ── 2. Pick an export file ────────────────────────────────────────────────────
step "Locating Dabble export file..."

EXPORTS_DIR="Exports"
mapfile -t export_files < <(find "$EXPORTS_DIR" -maxdepth 1 -name "*.json" 2>/dev/null | sort -r)

if [[ ${#export_files[@]} -eq 0 ]]; then
    echo "No export files found in $EXPORTS_DIR/."
    read -rp "  Enter the full path to your Dabble export JSON: " EXPORT_PATH
    [[ -f "$EXPORT_PATH" ]] || error "File not found: $EXPORT_PATH"
elif [[ ${#export_files[@]} -eq 1 ]]; then
    EXPORT_PATH="${export_files[0]}"
    echo "  Found: $EXPORT_PATH"
    read -rp "  Use this file? [Y/n] " yn
    if [[ "${yn,,}" == "n" ]]; then
        read -rp "  Enter the full path to your Dabble export JSON: " EXPORT_PATH
        [[ -f "$EXPORT_PATH" ]] || error "File not found: $EXPORT_PATH"
    fi
else
    echo "  Multiple export files found:"
    for i in "${!export_files[@]}"; do
        echo "    [$((i+1))] ${export_files[$i]}"
    done
    echo "    [c] Enter a custom path"
    while true; do
        read -rp "  Select [1]: " choice
        choice="${choice:-1}"
        if [[ "$choice" == "c" ]]; then
            read -rp "  Enter the full path to your Dabble export JSON: " EXPORT_PATH
            [[ -f "$EXPORT_PATH" ]] || error "File not found: $EXPORT_PATH"
            break
        elif [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#export_files[@]} )); then
            EXPORT_PATH="${export_files[$((choice-1))]}"
            break
        else
            warn "Invalid choice. Enter a number between 1 and ${#export_files[@]}, or 'c'."
        fi
    done
fi
ok "Using export: $EXPORT_PATH"

# ── 3. Database location ──────────────────────────────────────────────────────
step "Setting up SQLite database..."
DEFAULT_DB=".dabble-tasks/dabble.db"
read -rp "  Database path [$DEFAULT_DB]: " DB_PATH
DB_PATH="${DB_PATH:-$DEFAULT_DB}"
ok "Database will be created at: $DB_PATH"

# ── 4. Import ─────────────────────────────────────────────────────────────────
step "Importing export into SQLite (this may take a moment)..."
dabble-mcp import "$EXPORT_PATH" --db "$DB_PATH"
ok "Import complete"

# ── 5. Default project ────────────────────────────────────────────────────────
step "Choosing default project..."
echo "  Available projects:"
mapfile -t project_lines < <(dabble-mcp --db "$DB_PATH" list-projects 2>/dev/null | python3 -c "
import json, sys
projects = json.load(sys.stdin)
for i, p in enumerate(projects, 1):
    print(f'  [{i}] {p[\"title\"]} ({p[\"project_id\"]})')
" 2>/dev/null || true)

if [[ ${#project_lines[@]} -eq 0 ]]; then
    warn "Could not list projects. Skipping default project selection."
    PROJECT_ID=""
else
    printf '%s\n' "${project_lines[@]}"
    echo "    [s] Skip (no default project)"
    while true; do
        read -rp "  Select default project [s]: " choice
        choice="${choice:-s}"
        if [[ "$choice" == "s" ]]; then
            PROJECT_ID=""
            break
        elif [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#project_lines[@]} )); then
            PROJECT_ID=$(dabble-mcp --db "$DB_PATH" list-projects 2>/dev/null | python3 -c "
import json, sys
projects = json.load(sys.stdin)
idx = $choice - 1
print(projects[idx]['project_id'])
")
            break
        else
            warn "Invalid choice."
        fi
    done
fi

# ── 6. LLM configuration ──────────────────────────────────────────────────────
step "Configuring LLM for chapter summaries..."
echo "  Which LLM backend would you like to use?"
echo "    [1] OpenAI (requires OPENAI_API_KEY)"
echo "    [2] Local Ollama (requires Ollama running on localhost)"
echo "    [3] Other OpenAI-compatible API"
echo "    [s] Skip (configure later)"

while true; do
    read -rp "  Select [s]: " llm_choice
    llm_choice="${llm_choice:-s}"
    case "$llm_choice" in
        1)
            echo "  Available OpenAI models (common choices):"
            echo "    [1] gpt-5.4-mini  (fast, cheap)"
            echo "    [2] gpt-5.4       (best quality)"
            echo "    [3] Enter custom model name"
            read -rp "  Select model [1]: " model_choice
            model_choice="${model_choice:-1}"
            case "$model_choice" in
                1) MODEL="gpt-5.4-mini" ;;
                2) MODEL="gpt-5.4" ;;
                3) read -rp "  Model name: " MODEL ;;
                *) MODEL="gpt-5.4-mini" ;;
            esac
            BASE_URL=""
            if [[ -z "${OPENAI_API_KEY:-}" ]]; then
                warn "OPENAI_API_KEY is not set. Add it to your shell profile before running summaries."
            fi
            break
            ;;
        2)
            echo "  Available Ollama models (common choices):"
            echo "    [1] qwen2.5:14b-instruct  (recommended)"
            echo "    [2] llama3.2"
            echo "    [3] Enter custom model name"
            read -rp "  Select model [1]: " model_choice
            model_choice="${model_choice:-1}"
            case "$model_choice" in
                1) MODEL="qwen2.5:14b-instruct" ;;
                2) MODEL="llama3.2" ;;
                3) read -rp "  Model name: " MODEL ;;
                *) MODEL="qwen2.5:14b-instruct" ;;
            esac
            BASE_URL="http://localhost:11434/v1"
            break
            ;;
        3)
            read -rp "  Base URL (e.g. http://localhost:8080/v1): " BASE_URL
            read -rp "  Model name: " MODEL
            break
            ;;
        s|S)
            MODEL=""
            BASE_URL=""
            break
            ;;
        *)
            warn "Invalid choice. Enter 1, 2, 3, or s."
            ;;
    esac
done

# ── 7. Write defaults ─────────────────────────────────────────────────────────
step "Saving defaults..."
dabble-mcp set-defaults db "$DB_PATH" export "$EXPORT_PATH"

if [[ -n "$PROJECT_ID" ]]; then
    dabble-mcp set-defaults project "$PROJECT_ID"
fi
if [[ -n "${MODEL:-}" ]]; then
    dabble-mcp set-defaults model "$MODEL"
fi
if [[ -n "${BASE_URL:-}" ]]; then
    dabble-mcp set-defaults base-url "$BASE_URL"
fi

# ── 8. Summary ────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Setup complete!${RESET}"
echo "────────────────────────────────────────"
dabble-mcp list-defaults
echo ""
echo "Try it out:"
echo "  dabble-mcp list-projects"
if [[ -n "$PROJECT_ID" ]]; then
    echo "  dabble-mcp outline"
    echo "  dabble-mcp search \"your search term\""
fi
echo "  dabble-mcp serve"
echo ""
if [[ -n "${MODEL:-}" && -z "${BASE_URL:-}" ]]; then
    echo -e "${YELLOW}Remember to set OPENAI_API_KEY in your environment before running summaries.${RESET}"
fi
