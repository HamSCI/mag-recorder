#!/bin/bash
#
# mag-recorder config wizard (whiptail).
#
# Called by `mag-recorder config init` and `mag-recorder config edit`
# when stdout is a TTY and whiptail is installed.  Drives the operator
# through a small set of dialogs, validates inline, and writes the
# result through `mag-recorder config apply --json -` so the schema
# / type / range checks happen in Python.
#
# Usage:
#   config-wizard.sh init [--config <path>]
#   config-wizard.sh edit [--config <path>]
#
# Env (set by configurator.py before exec):
#   MAG_RECORDER_CLI         path to the mag-recorder binary to use
#   MAG_RECORDER_HELP_TOML   path to config/help.toml
#
# Reads (read-only) for pre-fills:
#   /etc/sigmond/coordination.env   STATION_* env bag (§14.3)
#
# The wizard never edits coordination.env; sigmond's own first-run
# interview owns that file.
#

set -euo pipefail

MODE="${1:-init}"; shift || true
CONFIG_PATH=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG_PATH="$2"; shift 2 ;;
        *) echo "config-wizard: unknown arg: $1" >&2; exit 2 ;;
    esac
done

MAG_RECORDER="${MAG_RECORDER_CLI:-mag-recorder}"
HELP_TOML="${MAG_RECORDER_HELP_TOML:-/opt/git/sigmond/mag-recorder/config/help.toml}"
COORD_ENV="/etc/sigmond/coordination.env"

# whiptail box sizing.
HEIGHT=20
WIDTH=78
LIST_HEIGHT=10
BACKTITLE="mag-recorder configuration"

# -------- preflight ----------------------------------------------------

if ! command -v whiptail >/dev/null 2>&1; then
    cat <<EOF >&2
mag-recorder config: whiptail is not installed on this host.

The interactive wizard requires it.  Install with:

    sudo apt install whiptail

Or use the legacy stdin-prompt path with:

    mag-recorder config $MODE --non-interactive
EOF
    exit 1
fi

# Pre-fill seed values from sigmond's coordination.env (read-only).
# We don't source the file -- it's a shell-style env file and sourcing
# arbitrary admin-controlled scripts is brittle and slightly unsafe;
# grep the keys we care about explicitly.
seed_from_coord_env() {
    local key="$1"
    [[ -r "$COORD_ENV" ]] || return 0
    # Match KEY=value or KEY="value" or KEY='value'; trim quotes.
    sed -nE "s|^[[:space:]]*${key}=([\"']?)([^\"']*)\\1[[:space:]]*\$|\\2|p" \
        "$COORD_ENV" | tail -1
}

# Helper: read the current effective config as JSON and pull one
# scalar out via python -m json.tool + key navigation.  Avoids
# bash-side TOML parsing.
config_get() {
    local section="$1" key="$2"
    "$MAG_RECORDER" config show --json --defaults ${CONFIG_PATH:+--config "$CONFIG_PATH"} 2>/dev/null \
        | python3 -c "
import json, sys
d = json.load(sys.stdin)
v = d.get('$section', {}).get('$key', '')
if isinstance(v, bool):
    print('true' if v else 'false')
else:
    print(v)
"
}

# In-session value lookup: check SCRATCH_JSON first (the operator's
# pending edits, not yet written), fall back to the on-disk config.
# Needed so re-entering a section via the main menu pre-fills with
# what the operator typed last time, not what's still on disk.
current_value() {
    local section="$1" key="$2"
    local scratch_val
    scratch_val=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
v = d.get('$section', {}).get('$key', None)
if v is None:
    pass    # signal 'not in scratch' by empty stdout
elif isinstance(v, bool):
    print('true' if v else 'false')
else:
    print(v)
")
    if [[ -n "$scratch_val" ]]; then
        echo "$scratch_val"
    else
        config_get "$section" "$key"
    fi
}

# Helper: pull help.toml's title / help / example / validator_hint
# for a dotted-key.  One python invocation per field (cheap enough).
help_get() {
    local dotted="$1" attr="$2"
    [[ -r "$HELP_TOML" ]] || return 0
    python3 -c "
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open('$HELP_TOML', 'rb') as f:
    d = tomllib.load(f)
# [station.psws_station_id] in TOML creates a NESTED table:
# d['station']['psws_station_id'], not a flat key.  Walk dotted path.
node = d
for part in '$dotted'.split('.'):
    if not isinstance(node, dict):
        node = {}
        break
    node = node.get(part, {})
if isinstance(node, dict):
    print(node.get('$attr', ''))
" 2>/dev/null
}

# Validators -- one bash function per format, returns 0 on success.

valid_psws_id()      { [[ "$1" =~ ^[Ss][0-9]{6}$ ]]; }
valid_callsign()     { [[ "$1" =~ ^[A-Za-z0-9/]{1,9}$ ]]; }
# Maidenhead: field (A-R) + square (0-9) + subsquare (a-x) + optional
# extended (0-9).  Accept any case here; the wizard canonicalizes the
# subsquare to lowercase before writing.
valid_grid()         { [[ "$1" =~ ^[A-Ra-r]{2}[0-9]{2}[A-Xa-x]{2}([0-9]{2})?$ ]]; }
# 7-bit I2C address space: 1..0x7F (0 is reserved, addresses above
# 0x7F don't exist on the wire).  Accept decimal (47) or hex (0x2F);
# range-check after parse.
valid_address_hex()  {
    [[ "$1" =~ ^(0[xX][0-9a-fA-F]{1,2}|[0-9]{1,3})$ ]] || return 1
    local n
    if [[ "$1" =~ ^0[xX] ]]; then
        n=$((16#${1#0[xX]}))
    else
        n=$((10#$1))
    fi
    (( n >= 1 && n <= 127 ))
}
valid_decimal()      { [[ "$1" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; }
valid_path_readable(){ [[ -r "$1" ]]; }
valid_int_range()    { local v="$1" lo="$2" hi="$3"; [[ "$v" =~ ^[0-9]+$ ]] && (( v >= lo && v <= hi )); }

# Ask one input box.  Returns the entered value on stdout, or
# returns 1 on operator-cancel.  Loops on validation failure.
#
# Args: dotted_key  current_value  validator_fn  validator_args...
ask() {
    local dotted="$1" current="$2" validator="$3"; shift 3
    local extra_args=("$@")

    local title; title=$(help_get "$dotted" "title")
    local help;  help=$(help_get  "$dotted" "help")
    local example;  example=$(help_get "$dotted" "example")
    local hint;  hint=$(help_get  "$dotted" "validator_hint")

    [[ -z "$title" ]] && title="$dotted"
    local body="$help"
    [[ -n "$hint"    ]] && body+=$'\n\nFormat: '"$hint"
    [[ -n "$example" ]] && body+=$'\n''Example: '"$example"

    local entered
    while :; do
        if ! entered=$(whiptail \
                --title "$title" \
                --backtitle "$BACKTITLE" \
                --inputbox "$body" \
                "$HEIGHT" "$WIDTH" \
                "$current" 3>&1 1>&2 2>&3); then
            return 1
        fi
        entered="${entered## }"; entered="${entered%% }"  # trim
        if "$validator" "$entered" "${extra_args[@]}"; then
            echo "$entered"
            return 0
        fi
        whiptail --title "Invalid value" \
                 --backtitle "$BACKTITLE" \
                 --msgbox $'That value didn'\''t match the expected format.\n\n'"Hint: ${hint:-(see help text)}" \
                 12 "$WIDTH"
        current="$entered"
    done
}

# -------- screens -----------------------------------------------------

welcome_screen() {
    local body
    if [[ "$MODE" == "init" ]]; then
        body="Welcome to the mag-recorder configuration wizard.

You'll see a menu of sections (Station, Uploader, Advanced); pick
any section to fill in, then return to the menu.  Pick 'Apply'
when you're done to write everything in one go, or 'Cancel' to
discard pending changes and exit.

Pre-fills come from /etc/sigmond/coordination.env (if present) and
your current /etc/mag-recorder/mag-recorder-config.toml.  Inside a
section, pressing Cancel drops back to the menu (not all the way
out) -- effectively a 'back' button."
    else
        body="Edit the current mag-recorder configuration.

You'll see a menu of sections (Station, Uploader, Advanced) with
current values shown inline.  Pick any section to edit, then
return to the menu.  Pick 'Apply' to write changes or 'Cancel' to
discard them.  Inside a section, pressing Cancel drops back to
the menu (not out)."
    fi
    whiptail --title "mag-recorder configuration wizard" \
             --backtitle "$BACKTITLE" \
             --yesno "$body"$'\n\n'"Continue?" \
             "$HEIGHT" "$WIDTH"
}

# Walk one section's required fields, building up a JSON fragment.
# Sets global SCRATCH_JSON to the assembled section.
collect_station() {
    local psws_id callsign grid instrument description latitude longitude elevation

    psws_id=$(current_value station psws_station_id)
    [[ -z "$psws_id" || "$psws_id" == "<YOUR_PSWS_STATION_ID>" ]] \
        && psws_id="$(seed_from_coord_env STATION_PSWS_STATION_ID)"

    callsign=$(current_value station callsign)
    [[ -z "$callsign" || "$callsign" == "<YOUR_CALL>" ]] \
        && callsign="$(seed_from_coord_env STATION_CALLSIGN)"

    grid=$(current_value station grid_square)
    [[ -z "$grid" || "$grid" == "<YOUR_GRID>" ]] \
        && grid="$(seed_from_coord_env STATION_GRID)"

    instrument=$(current_value station instrument_id)
    [[ -z "$instrument" ]] && instrument="RM3100"

    description=$(current_value station description)

    psws_id=$(ask    station.psws_station_id "$psws_id"    valid_psws_id)    || return 1
    # PSWS IDs are canonical with uppercase 'S'.
    psws_id="S${psws_id:1}"
    callsign=$(ask   station.callsign        "$callsign"   valid_callsign)   || return 1
    callsign="${callsign^^}"
    grid=$(ask       station.grid_square     "$grid"       valid_grid)       || return 1
    # Canonical Maidenhead: field uppercase, square unchanged (digits),
    # subsquare lowercase, extended square (if present) unchanged.
    {
        local _f="${grid:0:2}" _s="${grid:2:2}" _ss="${grid:4:2}" _ex="${grid:6:2}"
        grid="${_f^^}${_s}${_ss,,}${_ex}"
    }
    instrument=$(ask station.instrument_id   "$instrument" valid_callsign)   || return 1
    instrument="${instrument^^}"

    # Description is free-form; no validator.
    if description=$(ask station.description "$description" true); then
        :
    else
        return 1
    fi

    # Lat/long/elev are optional.  Pre-fill from coordination.env or
    # the current TOML; operator who wants to skip just presses
    # Enter through (or types over).  The yesno gate that used to
    # live here was dead weight -- three inputbox dialogs with sane
    # defaults are themselves the "skip" affordance.
    local lat lon elev
    lat=$(current_value  station latitude)
    [[ -z "$lat"  || "$lat"  == "0.0" ]] && lat=$(seed_from_coord_env  STATION_LATITUDE)
    lon=$(current_value  station longitude)
    [[ -z "$lon"  || "$lon"  == "0.0" ]] && lon=$(seed_from_coord_env  STATION_LONGITUDE)
    elev=$(current_value station elevation_m)
    [[ -z "$elev" || "$elev" == "0.0" ]] && elev=$(seed_from_coord_env STATION_ELEVATION_M)
    latitude=$(ask   station.latitude    "${lat:-0.0}"  valid_decimal) || return 1
    longitude=$(ask  station.longitude   "${lon:-0.0}"  valid_decimal) || return 1
    elevation=$(ask  station.elevation_m "${elev:-0.0}" valid_decimal) || return 1

    SCRATCH_JSON=$(python3 -c "
import json
print(json.dumps({
    'station': {
        'psws_station_id': '$psws_id',
        'callsign':        '$callsign',
        'grid_square':     '$grid',
        'instrument_id':   '$instrument',
        'description':     '''$description''',
        'latitude':        float('${latitude:-0.0}'),
        'longitude':       float('${longitude:-0.0}'),
        'elevation_m':     float('${elevation:-0.0}'),
    },
}))
")
}

collect_uploader() {
    local user ssh_key bw default_user
    user=$(current_value uploader user)
    default_user=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
print(d.get('station', {}).get('psws_station_id', '') or '')
")
    [[ -z "$default_user" ]] && default_user=$(current_value station psws_station_id)
    [[ -z "$user" ]] && user="$default_user"
    ssh_key=$(current_value uploader ssh_key_file)
    bw=$(current_value uploader bandwidth_limit_kbps)

    user=$(ask    uploader.user           "$user"    valid_callsign)    || return 1
    ssh_key=$(ask uploader.ssh_key_file   "$ssh_key" valid_path_readable) || return 1
    # 0 = unlimited (mag_recorder.core.uploader translates 0 -> None
    # before passing to PswsMagnetometerSftp, which omits the sftp -l flag).
    bw=$(ask      uploader.bandwidth_limit_kbps "$bw" valid_int_range 0 1000000) || return 1

    SCRATCH_JSON=$(python3 -c "
import json
d = json.loads(r'''$SCRATCH_JSON''')
d['uploader'] = {
    'user':                 '$user',
    'ssh_key_file':         '$ssh_key',
    'bandwidth_limit_kbps': int('$bw'),
}
print(json.dumps(d))
")
}

collect_advanced() {
    # The main menu's "Advanced" choice is itself the opt-in for this
    # section -- no separate yesno gate needed.  These knobs (chip
    # I2C address, cycle count, NOS averaging, sampling mode, TMRC
    # rate, device path) have defaults matching upstream mag-usb;
    # most operators won't need to touch any of them.
    local addr cc nos mode tmrc device
    addr=$(current_value mag i2c_address)
    cc=$(current_value   mag cycle_count)
    nos=$(current_value  mag nos)
    mode=$(current_value mag sampling_mode)
    tmrc=$(current_value mag tmrc_rate)
    device=$(current_value mag device)

    addr=$(ask   mag.i2c_address    "$(printf '0x%02X' "$addr")" valid_address_hex)  || return 1
    addr=$((addr))   # normalize 0xNN / NN to a decimal int
    cc=$(ask     mag.cycle_count    "$cc"   valid_int_range 1 800)                  || return 1
    nos=$(ask    mag.nos            "$nos"  valid_int_range 1 255)                  || return 1
    mode=$(whiptail --title "RM3100 sampling mode" \
                    --backtitle "$BACKTITLE" \
                    --menu "$(help_get mag.sampling_mode help)" \
                    "$HEIGHT" "$WIDTH" 2 \
                    "POLL" "single-shot, host-triggered (default)" \
                    "CMM"  "continuous measurement (unused today)" \
                    3>&1 1>&2 2>&3) || return 1
    tmrc=$(ask   mag.tmrc_rate      "$(printf '0x%02X' "$tmrc")" valid_address_hex) || return 1
    tmrc=$((tmrc))
    device=$(ask mag.device         "$device" true)                                  || return 1

    SCRATCH_JSON=$(python3 -c "
import json
d = json.loads(r'''$SCRATCH_JSON''')
d.setdefault('mag', {}).update({
    'i2c_address':    int('$addr'),
    'cycle_count':    int('$cc'),
    'nos':            int('$nos'),
    'sampling_mode':  '$mode',
    'tmrc_rate':      int('$tmrc'),
    'device':         '$device',
})
print(json.dumps(d))
")
}

main_menu_loop() {
    # Top-level menu; "back" is implicit (Cancel within any section
    # drops back here instead of aborting the wizard).  Exit codes:
    #   0  applied and exit
    #   1  cancelled (no write)
    # Normalize a value for menu display: empty string or a leftover
    # template placeholder (`<YOUR_FOO>`) both render as "(unset)" so
    # the menu doesn't show ugly angle-bracket strings.
    display() {
        local v="$1"
        if [[ -z "$v" || "$v" =~ ^\<.*\>$ ]]; then
            echo "(unset)"
        else
            echo "$v"
        fi
    }

    while :; do
        # Build menu items showing current values inline, so the
        # operator sees state at a glance without entering each section.
        local cur_psws cur_call cur_grid cur_user cur_addr cur_cc
        cur_psws=$(display "$(current_value station psws_station_id)")
        cur_call=$(display "$(current_value station callsign)")
        cur_grid=$(display "$(current_value station grid_square)")
        cur_user=$(display "$(current_value uploader user)")
        cur_addr=$(current_value mag i2c_address)
        cur_cc=$(current_value   mag cycle_count)

        # Format addr as 0xNN for visual consistency with the rest of the UI.
        local cur_addr_hex
        if [[ "$cur_addr" =~ ^[0-9]+$ ]]; then
            cur_addr_hex=$(printf '0x%02X' "$cur_addr")
        else
            cur_addr_hex="$cur_addr"
        fi

        local choice
        choice=$(whiptail --title "mag-recorder configuration" \
                          --backtitle "$BACKTITLE" \
                          --cancel-button "Exit wizard" \
                          --menu "Pick a section to edit, or Apply when you're done.

Each section's questions walk linearly; Cancel inside a section
drops back here instead of aborting." \
                          "$HEIGHT" "$WIDTH" 5 \
                          "Station"  "PSWS=$cur_psws  Call=$cur_call  Grid=$cur_grid" \
                          "Uploader" "user=$cur_user" \
                          "Advanced" "addr=$cur_addr_hex  cc=$cur_cc" \
                          "Apply"    "Review and write changes" \
                          "Cancel"   "Discard changes and exit" \
                          3>&1 1>&2 2>&3)
        # Esc / hard-cancel on the menu itself: confirm before discarding.
        if [[ $? -ne 0 ]]; then
            if whiptail --title "Discard changes?" \
                        --backtitle "$BACKTITLE" \
                        --yesno "Discard any pending changes and exit the wizard?" \
                        10 "$WIDTH"; then
                return 1
            fi
            continue
        fi

        case "$choice" in
            Station)
                collect_station || true   # Cancel inside drops back to menu
                ;;
            Uploader)
                collect_uploader || true
                ;;
            Advanced)
                collect_advanced || true
                ;;
            Apply)
                # confirm_and_write returns 0 on success, 1 if the
                # operator cancelled the final review.  On success we
                # exit the wizard; on cancel we stay in the menu.
                if confirm_and_write; then
                    return 0
                fi
                ;;
            Cancel)
                if whiptail --title "Discard changes?" \
                            --backtitle "$BACKTITLE" \
                            --yesno "Discard any pending changes and exit the wizard?" \
                            10 "$WIDTH"; then
                    return 1
                fi
                ;;
        esac
    done
}

confirm_and_write() {
    local summary
    summary=$(python3 -c "
import json
d = json.loads(r'''$SCRATCH_JSON''')
lines = []
def walk(prefix, obj):
    for k, v in obj.items():
        if isinstance(v, dict):
            walk(prefix + k + '.', v)
        else:
            lines.append(f'{prefix}{k} = {v!r}')
walk('', d)
print('\n'.join(lines))
")
    whiptail --title "Review and write" \
             --backtitle "$BACKTITLE" \
             --yesno "About to apply the following to ${CONFIG_PATH:-/etc/mag-recorder/mag-recorder-config.toml}:

$summary

Continue?" \
             "$HEIGHT" "$WIDTH" || return 1

    if ! printf '%s' "$SCRATCH_JSON" | \
            "$MAG_RECORDER" config apply --json - ${CONFIG_PATH:+--config "$CONFIG_PATH"}; then
        whiptail --title "Apply failed" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "mag-recorder config apply rejected the input.

See stderr for details (scroll back in the terminal).  Your
existing config file was not modified." \
                 12 "$WIDTH"
        return 1
    fi
    whiptail --title "Config written" \
             --backtitle "$BACKTITLE" \
             --msgbox "Configuration written.

Next steps:
  - Verify with:     mag-recorder validate --json
  - Validate chip:   MAG_RECORDER_VALIDATE_CHIP=1 mag-recorder validate --json
  - Start daemon:    sudo systemctl restart mag-recorder.service" \
             "$HEIGHT" "$WIDTH"
}

# -------- main flow ---------------------------------------------------

SCRATCH_JSON='{}'

welcome_screen || { echo "wizard: cancelled at welcome" >&2; exit 1; }
if main_menu_loop; then
    exit 0
else
    echo "wizard: exited without writing" >&2
    exit 1
fi
