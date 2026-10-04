#!/bin/bash
# SkillWeave Online Installer
# One-line installer: curl -s https://raw.githubusercontent.com/SkillWeave-AI/skillweave/main/install.sh | bash
#
# This script downloads the latest SkillWeave release and installs it to all detected AI agents.
# It supports offline installation if the repository is already cloned locally.

set -e

SKILLWEAVE_REPO="https://github.com/SkillWeave-AI/skillweave.git"
TEMP_DIR="/tmp/skillweave-install-$(date +%s)"
INSTALL_SCRIPT="scripts/install-skills.sh"
DEFAULT_BRANCH="main"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
    echo -e "${BLUE}[SkillWeave]${NC} $1"
}

success() {
    echo -e "${GREEN}✓${NC} $1"
}

warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

error() {
    echo -e "${RED}✗${NC} $1" >&2
}

# Check for required commands
check_requirements() {
    local missing=()
    for cmd in git curl; do
        if ! command -v $cmd &> /dev/null; then
            missing+=("$cmd")
        fi
    done
    
    if [ ${#missing[@]} -gt 0 ]; then
        error "Missing required commands: ${missing[*]}"
        error "Please install them and try again."
        exit 1
    fi
}

# Check if we're already in a SkillWeave repository
is_skillweave_repo() {
    [ -f "scripts/install-skills.sh" ] && [ -d "skills" ]
}

# Clone repository to temporary directory
clone_repo() {
    log "Cloning SkillWeave repository..."
    git clone --depth 1 --branch "$DEFAULT_BRANCH" "$SKILLWEAVE_REPO" "$TEMP_DIR" 2>/dev/null || {
        error "Failed to clone repository"
        exit 1
    }
    success "Repository cloned to $TEMP_DIR"
}

# Run installer with provided arguments
run_installer() {
    local install_dir="$1"
    shift
    local args=("$@")
    
    log "Running installer with arguments: ${args[*]}"
    
    cd "$install_dir"
    
    if [ ! -f "$INSTALL_SCRIPT" ]; then
        error "Installer script not found: $INSTALL_SCRIPT"
        return 1
    fi
    
    chmod +x "$INSTALL_SCRIPT"
    "./$INSTALL_SCRIPT" "${args[@]}"
}

# Clean up temporary directory
cleanup() {
    if [ -d "$TEMP_DIR" ]; then
        log "Cleaning up temporary directory..."
        rm -rf "$TEMP_DIR"
        success "Cleanup complete"
    fi
}

# Main installation process
main() {
    trap cleanup EXIT
    
    log "Starting SkillWeave installation..."
    log "Repository: $SKILLWEAVE_REPO"
    log "Branch: $DEFAULT_BRANCH"
    echo
    
    check_requirements
    
    local install_dir
    local is_temp=false
    
    # Determine installation source
    if is_skillweave_repo; then
        install_dir="$(pwd)"
        success "Using existing SkillWeave repository at $install_dir"
    else
        clone_repo
        install_dir="$TEMP_DIR"
        is_temp=true
    fi
    
    echo
    log "Starting SkillWeave installer..."
    echo
    
    # Run installer with all passed arguments
    run_installer "$install_dir" "$@"
    
    if [ "$is_temp" = true ]; then
        echo
        log "Note: The SkillWeave repository was cloned to a temporary directory."
        log "To update skills in the future, run:"
        log "  curl -s https://raw.githubusercontent.com/LangeVC/skillweave/main/install.sh | bash"
    else
        echo
        log "Note: You're running from a local SkillWeave repository."
        log "To update skills in the future, run from this directory:"
        log "  ./scripts/install-skills.sh --update"
    fi
    
    echo
    success "Installation complete!"
}

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --help|-h)
                echo "SkillWeave Online Installer"
                echo "Usage: curl -s https://raw.githubusercontent.com/LangeVC/skillweave/main/install.sh | bash -s -- [options]"
                echo
                echo "Options:"
                echo "  --interactive, -i   Interactive agent selection"
                echo "  --dry-run           Preview changes without making them"
                echo "  --uninstall         Uninstall skills from selected agents"
                echo "  --update            Update existing installations"
                echo "  --troubleshoot      Diagnose installation issues"
                echo "  --list              List detected agents"
                echo "  --init              Initialize Next Level features in current project"
                echo "  --help, -h          Show this help message"
                echo
                echo "Examples:"
                echo "  # Interactive installation"
                echo "  curl -s https://raw.githubusercontent.com/LangeVC/skillweave/main/install.sh | bash -s -- --interactive"
                echo
                echo "  # Dry-run to preview changes"
                echo "  curl -s https://raw.githubusercontent.com/LangeVC/skillweave/main/install.sh | bash -s -- --dry-run"
                echo
                echo "  # Initialize Next Level features in current project"
                echo "  curl -s https://raw.githubusercontent.com/LangeVC/skillweave/main/install.sh | bash -s -- --init"
                exit 0
                ;;
            *)
                # All other arguments are passed to the installer
                break
                ;;
        esac
    done
}

# If script is being executed (not sourced), run main
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    parse_args "$@"
    main "$@"
fi