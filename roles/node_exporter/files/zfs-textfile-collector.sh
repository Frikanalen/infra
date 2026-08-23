#!/usr/bin/env bash
#
# Writes ZFS pool, scrub, error and dataset metrics into node_exporter's
# textfile collector directory. Installed and scheduled by
# roles/node_exporter -- edits here are overwritten on the next Ansible run.
#
# Everything below comes from zpool(8)/zfs(8) rather than from the kstats in
# /proc/spl/kstat/zfs. node_exporter's own zfs collector already reads those,
# and they carry the ARC and the per-pool io counters but nothing about
# capacity, fragmentation, scrubs, device errors or datasets.
#
# The file is written to a temporary name and rename(2)d into place: the
# textfile collector will happily read a half-written file and report the
# truncated last line as a parse error, and an atomic rename is the documented
# way to avoid it.

set -uo pipefail
export LC_ALL=C
# zpool and zfs live in /sbin on Debian, which is not always on the
# inherited PATH; appended rather than replacing it so the script stays
# runnable by hand from anywhere.
export PATH="${PATH}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

OUT_DIR="${1:?usage: $0 <textfile-directory>}"
OUT="${OUT_DIR}/zfs.prom"
TMP="$(mktemp "${OUT}.XXXXXX")"
trap 'rm -f "${TMP}"' EXIT

start="$(date +%s.%N)"
ok=1

emit() { printf '%s\n' "$*" >>"${TMP}"; }

# Prometheus label values are quoted strings: backslash and double quote have
# to be escaped. Dataset names can legally contain neither, but pool and
# device names come off a live system and this is one line.
esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

if ! command -v zpool >/dev/null 2>&1; then
    # Nothing to do, but say so positively rather than leaving a stale file
    # behind from before the pools went away.
    emit '# HELP zfs_scrape_success Whether the ZFS textfile collector ran successfully.'
    emit '# TYPE zfs_scrape_success gauge'
    emit 'zfs_scrape_success 1'
    mv "${TMP}" "${OUT}"
    chmod 0644 "${OUT}"
    trap - EXIT
    exit 0
fi

# zpool status -p prints exact error counters instead of the human-readable
# "1.2K". It has been there since 0.8, but fall back rather than produce
# nothing if this host predates it.
status_flags="-p"
zpool status -p >/dev/null 2>&1 || status_flags=""

# --- pools ------------------------------------------------------------------

emit '# HELP zfs_pool_health Pool health as reported by zpool list, 1 for the current state.'
emit '# TYPE zfs_pool_health gauge'
emit '# HELP zfs_pool_size_bytes Total size of the pool.'
emit '# TYPE zfs_pool_size_bytes gauge'
emit '# HELP zfs_pool_allocated_bytes Space allocated in the pool.'
emit '# TYPE zfs_pool_allocated_bytes gauge'
emit '# HELP zfs_pool_free_bytes Space free in the pool.'
emit '# TYPE zfs_pool_free_bytes gauge'
emit '# HELP zfs_pool_capacity_ratio Fraction of the pool that is allocated, 0-1.'
emit '# TYPE zfs_pool_capacity_ratio gauge'
emit '# HELP zfs_pool_fragmentation_ratio Fragmentation of the pool free space, 0-1.'
emit '# TYPE zfs_pool_fragmentation_ratio gauge'
emit '# HELP zfs_pool_dedup_ratio Deduplication ratio of the pool.'
emit '# TYPE zfs_pool_dedup_ratio gauge'

pools=()
pool_list="$(zpool list -Hp -o name,size,alloc,free,frag,cap,dedup,health 2>/dev/null)"
if [ $? -ne 0 ]; then
    ok=0
    pool_list=""
fi

while IFS=$'\t' read -r name size alloc free frag cap dedup health; do
    [ -n "${name:-}" ] || continue
    pools+=("${name}")
    n="$(esc "${name}")"
    emit "zfs_pool_health{pool=\"${n}\",health=\"$(esc "${health}")\"} 1"
    # A pool with no data reports "-" for frag and dedup; skip those rather
    # than emitting a metric that will not parse.
    [ "${size}" != "-" ]  && emit "zfs_pool_size_bytes{pool=\"${n}\"} ${size}"
    [ "${alloc}" != "-" ] && emit "zfs_pool_allocated_bytes{pool=\"${n}\"} ${alloc}"
    [ "${free}" != "-" ]  && emit "zfs_pool_free_bytes{pool=\"${n}\"} ${free}"
    [ "${cap}" != "-" ]   && emit "zfs_pool_capacity_ratio{pool=\"${n}\"} $(awk -v v="${cap}" 'BEGIN{printf "%.4f", v/100}')"
    [ "${frag}" != "-" ]  && emit "zfs_pool_fragmentation_ratio{pool=\"${n}\"} $(awk -v v="${frag}" 'BEGIN{printf "%.4f", v/100}')"
    [ "${dedup}" != "-" ] && emit "zfs_pool_dedup_ratio{pool=\"${n}\"} ${dedup}"
done <<<"${pool_list}"

# --- scrubs, resilvers and errors -------------------------------------------

emit '# HELP zfs_pool_last_scrub_timestamp_seconds Completion time of the last finished scrub, 0 if the pool has never been scrubbed.'
emit '# TYPE zfs_pool_last_scrub_timestamp_seconds gauge'
emit '# HELP zfs_pool_last_scrub_errors Unrepairable errors found by the last finished scrub.'
emit '# TYPE zfs_pool_last_scrub_errors gauge'
emit '# HELP zfs_pool_scrub_in_progress Whether a scrub is running right now.'
emit '# TYPE zfs_pool_scrub_in_progress gauge'
emit '# HELP zfs_pool_resilver_in_progress Whether a resilver is running right now.'
emit '# TYPE zfs_pool_resilver_in_progress gauge'
emit '# HELP zfs_pool_data_errors Number of known data errors in the pool.'
emit '# TYPE zfs_pool_data_errors gauge'
emit '# HELP zfs_pool_device_errors_total Per-device error counters as reported by zpool status.'
emit '# TYPE zfs_pool_device_errors_total counter'
emit '# HELP zfs_pool_device_state Per-device state, 1 for the current state.'
emit '# TYPE zfs_pool_device_state gauge'

for pool in "${pools[@]:-}"; do
    [ -n "${pool}" ] || continue
    n="$(esc "${pool}")"
    st="$(zpool status ${status_flags} "${pool}" 2>/dev/null)" || { ok=0; continue; }

    scan_line="$(printf '%s\n' "${st}" | sed -n 's/^[[:space:]]*scan:[[:space:]]*//p' | head -n 1)"

    scrub_running=0
    resilver_running=0
    case "${scan_line}" in
        "scrub in progress"*)    scrub_running=1 ;;
        "resilver in progress"*) resilver_running=1 ;;
    esac
    emit "zfs_pool_scrub_in_progress{pool=\"${n}\"} ${scrub_running}"
    emit "zfs_pool_resilver_in_progress{pool=\"${n}\"} ${resilver_running}"

    # "scrub repaired 0B in 03:12:45 with 0 errors on Sun Aug 10 04:12:00 2025"
    # is the only shape that carries a completion time; "none requested",
    # "scrub canceled" and an in-progress scan do not, and are reported as 0 so
    # that "never scrubbed" and "scrubbed far too long ago" are the same alert.
    last_scrub=0
    last_errors=0
    case "${scan_line}" in
        "scrub repaired"*" on "*)
            when="${scan_line##* on }"
            last_scrub="$(date -d "${when}" +%s 2>/dev/null || echo 0)"
            errs="$(printf '%s' "${scan_line}" | sed -n 's/.*with \([0-9][0-9]*\) errors.*/\1/p')"
            last_errors="${errs:-0}"
            ;;
    esac
    emit "zfs_pool_last_scrub_timestamp_seconds{pool=\"${n}\"} ${last_scrub}"
    emit "zfs_pool_last_scrub_errors{pool=\"${n}\"} ${last_errors}"

    # "errors: No known data errors", or "errors: 3 data errors, use '-v' ..."
    err_line="$(printf '%s\n' "${st}" | sed -n 's/^errors:[[:space:]]*//p' | head -n 1)"
    case "${err_line}" in
        "No known data errors") data_errors=0 ;;
        [0-9]*)                 data_errors="${err_line%% *}" ;;
        "")                     data_errors=0 ;;
        *)                      data_errors=1 ;;
    esac
    emit "zfs_pool_data_errors{pool=\"${n}\"} ${data_errors}"

    # The device table: everything between the "NAME STATE READ WRITE CKSUM"
    # header and the following blank line. Rows are vdevs and leaf devices at
    # varying indentation; both are worth having, since a raidz vdev going
    # DEGRADED and a single disk racking up checksum errors are different
    # problems. The pool's own row is skipped -- zfs_pool_health above is the
    # same number, from a source that does not depend on parsing a table.
    printf '%s\n' "${st}" | awk -v pool="${pool}" '
        /^[[:space:]]*NAME[[:space:]]+STATE[[:space:]]+READ[[:space:]]+WRITE[[:space:]]+CKSUM/ { intable = 1; next }
        intable && /^[[:space:]]*$/ { intable = 0 }
        intable && NF >= 5 {
            dev = $1; state = $2; r = $3; w = $4; c = $5
            if (dev == pool) next
            if (r !~ /^[0-9]+$/ || w !~ /^[0-9]+$/ || c !~ /^[0-9]+$/) next
            print dev "\t" state "\t" r "\t" w "\t" c
        }
    ' | while IFS=$'\t' read -r dev state r w c; do
        d="$(esc "${dev}")"
        emit "zfs_pool_device_state{pool=\"${n}\",device=\"${d}\",state=\"$(esc "${state}")\"} 1"
        emit "zfs_pool_device_errors_total{pool=\"${n}\",device=\"${d}\",type=\"read\"} ${r}"
        emit "zfs_pool_device_errors_total{pool=\"${n}\",device=\"${d}\",type=\"write\"} ${w}"
        emit "zfs_pool_device_errors_total{pool=\"${n}\",device=\"${d}\",type=\"checksum\"} ${c}"
    done
done

# --- datasets ---------------------------------------------------------------
#
# compressratio is deliberately absent: its parsable form has changed shape
# between ZFS releases, and logicalused/used below is the same number computed
# from two values whose units are unambiguous.

emit '# HELP zfs_dataset_used_bytes Space used by the dataset and its descendants.'
emit '# TYPE zfs_dataset_used_bytes gauge'
emit '# HELP zfs_dataset_available_bytes Space available to the dataset.'
emit '# TYPE zfs_dataset_available_bytes gauge'
emit '# HELP zfs_dataset_referenced_bytes Space referenced by the dataset itself.'
emit '# TYPE zfs_dataset_referenced_bytes gauge'
emit '# HELP zfs_dataset_logical_used_bytes Logical (pre-compression) space used by the dataset.'
emit '# TYPE zfs_dataset_logical_used_bytes gauge'
emit '# HELP zfs_dataset_used_by_snapshots_bytes Space used by snapshots of the dataset.'
emit '# TYPE zfs_dataset_used_by_snapshots_bytes gauge'
emit '# HELP zfs_dataset_quota_bytes Dataset quota, 0 if none is set.'
emit '# TYPE zfs_dataset_quota_bytes gauge'

ds_list="$(zfs list -Hp -t filesystem,volume \
    -o name,type,used,available,referenced,logicalused,usedbysnapshots,quota 2>/dev/null)"
if [ $? -ne 0 ]; then
    ok=0
    ds_list=""
fi

while IFS=$'\t' read -r name type used avail refer lused usnap quota; do
    [ -n "${name:-}" ] || continue
    d="$(esc "${name}")"
    p="$(esc "${name%%/*}")"
    l="pool=\"${p}\",dataset=\"${d}\",type=\"$(esc "${type}")\""
    [ "${used}"  != "-" ] && emit "zfs_dataset_used_bytes{${l}} ${used}"
    [ "${avail}" != "-" ] && emit "zfs_dataset_available_bytes{${l}} ${avail}"
    [ "${refer}" != "-" ] && emit "zfs_dataset_referenced_bytes{${l}} ${refer}"
    [ "${lused}" != "-" ] && emit "zfs_dataset_logical_used_bytes{${l}} ${lused}"
    [ "${usnap}" != "-" ] && emit "zfs_dataset_used_by_snapshots_bytes{${l}} ${usnap}"
    [ "${quota}" != "-" ] && emit "zfs_dataset_quota_bytes{${l}} ${quota}"
done <<<"${ds_list}"

# --- collector's own health -------------------------------------------------
#
# Staleness is covered by node_textfile_mtime_seconds, which node_exporter
# exports for every file in the directory; this is for the case where the
# script ran but zpool or zfs failed underneath it.

emit '# HELP zfs_scrape_success Whether the ZFS textfile collector ran successfully.'
emit '# TYPE zfs_scrape_success gauge'
emit "zfs_scrape_success ${ok}"
emit '# HELP zfs_scrape_duration_seconds Time the ZFS textfile collector took to run.'
emit '# TYPE zfs_scrape_duration_seconds gauge'
emit "zfs_scrape_duration_seconds $(awk -v s="${start}" -v e="$(date +%s.%N)" 'BEGIN{printf "%.3f", e-s}')"

chmod 0644 "${TMP}"
mv "${TMP}" "${OUT}"
trap - EXIT
