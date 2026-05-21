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

This wizard will walk you through the four PSWS-required fields,
then offer an optional advanced-tuning section for the RM3100
chip-side knobs.

Pre-fills come from /etc/sigmond/coordination.env (if present) and
your current /etc/mag-recorder/mag-recorder-config.toml.

Press <Tab> to move between fields and buttons.  Pressing <Esc> or
choosing Cancel at any prompt aborts without writing."
    else
        body="Edit the current mag-recorder configuration.

You'll see the existing value pre-filled in each box; change only
what you need to.  Press Cancel at any prompt to abort without
writing -- partial input is discarded."
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

    psws_id=$(config_get station psws_station_id)
    [[ -z "$psws_id" || "$psws_id" == "<YOUR_PSWS_STATION_ID>" ]] \
        && psws_id="$(seed_from_coord_env STATION_PSWS_STATION_ID)"

    callsign=$(config_get station callsign)
    [[ -z "$callsign" || "$callsign" == "<YOUR_CALL>" ]] \
        && callsign="$(seed_from_coord_env STATION_CALLSIGN)"

    grid=$(config_get station grid_square)
    [[ -z "$grid" || "$grid" == "<YOUR_GRID>" ]] \
        && grid="$(seed_from_coord_env STATION_GRID)"

    instrument=$(config_get station instrument_id)
    [[ -z "$instrument" ]] && instrument="RM3100"

    description=$(config_get station description)

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

    # Lat/long/elev are optional; only ask if the operator wants to set them.
    if whiptail --title "Optional: geodesy" \
                --backtitle "$BACKTITLE" \
                --yesno "Set station latitude / longitude / elevation now?

These are optional; PSWS uses them for metadata.  Skip if you'll
edit them later or accept the defaults of 0.0/0.0/0.0." \
                12 "$WIDTH"; then
        local lat lon elev
        lat=$(config_get  station latitude);      [[ -z "$lat"  || "$lat"  == "0.0" ]] && lat=$(seed_from_coord_env  STATION_LATITUDE)
        lon=$(config_get  station longitude);     [[ -z "$lon"  || "$lon"  == "0.0" ]] && lon=$(seed_from_coord_env  STATION_LONGITUDE)
        elev=$(config_get station elevation_m);   [[ -z "$elev" || "$elev" == "0.0" ]] && elev=$(seed_from_coord_env STATION_ELEVATION_M)
        latitude=$(ask   station.latitude    "${lat:-0.0}"  valid_decimal) || return 1
        longitude=$(ask  station.longitude   "${lon:-0.0}"  valid_decimal) || return 1
        elevation=$(ask  station.elevation_m "${elev:-0.0}" valid_decimal) || return 1
    else
        latitude=$(config_get  station latitude)
        longitude=$(config_get station longitude)
        elevation=$(config_get station elevation_m)
    fi

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
    user=$(config_get uploader user)
    default_user=$(python3 -c "
import json
d = json.loads(r'''$SCRATCH_JSON''')
print(d['station']['psws_station_id'])
")
    [[ -z "$user" ]] && user="$default_user"
    ssh_key=$(config_get uploader ssh_key_file)
    bw=$(config_get uploader bandwidth_limit_kbps)

    user=$(ask    uploader.user           "$user"    valid_callsign)    || return 1
    ssh_key=$(ask uploader.ssh_key_file   "$ssh_key" valid_path_readable) || return 1
    bw=$(ask      uploader.bandwidth_limit_kbps "$bw" valid_int_range 1 1000000) || return 1

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
    whiptail --title "Advanced RM3100 tuning" \
             --backtitle "$BACKTITLE" \
             --yesno "Open the advanced-tuning section?

These knobs (chip I2C address, cycle count, NOS averaging,
sampling mode, TMRC rate, device path) have defaults matching
upstream mag-usb.  Skip unless you've got a non-standard carrier
board or want to tune sensitivity / cadence.

Choose No to keep current values." \
             14 "$WIDTH" || return 0

    local addr cc nos mode tmrc device
    addr=$(config_get mag i2c_address)
    cc=$(config_get   mag cycle_count)
    nos=$(config_get  mag nos)
    mode=$(config_get mag sampling_mode)
    tmrc=$(config_get mag tmrc_rate)
    device=$(config_get mag device)

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

welcome_screen     || { echo "wizard: cancelled at welcome" >&2; exit 1; }
collect_station    || { echo "wizard: cancelled in station section" >&2; exit 1; }
collect_uploader   || { echo "wizard: cancelled in uploader section" >&2; exit 1; }
collect_advanced   || { echo "wizard: cancelled in advanced section" >&2; exit 1; }
confirm_and_write  || { echo "wizard: not written" >&2; exit 1; }

exit 0
