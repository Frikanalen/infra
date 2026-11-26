#!/usr/bin/env bash
#
# Writes NVIDIA GPU metrics from nvidia-smi into node_exporter's textfile
# collector directory. Installed and scheduled by roles/node_exporter on hosts
# that have nvidia-smi -- edits here are overwritten on the next Ansible run.
#
# nvidia-smi rather than DCGM: DCGM does not support GeForce cards, and
# caspar1's is a GT 1030. Not every field exists on every card or driver, and
# nvidia-smi rejects the whole query if one name is unknown, so the field list
# is filtered against --help-query-gpu first, and "[N/A]"/"[Not Supported]"
# values are skipped rather than written as garbage.
#
# nvidia_smi_up is the metric that matters most on caspar1: after a driver
# package upgrade without a module reload, nvidia-smi fails with a
# driver/library version mismatch and CasparCG silently falls back to
# llvmpipe software rendering.

set -uo pipefail
export LC_ALL=C
export PATH="${PATH}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

OUT_DIR="${1:?usage: $0 <textfile-directory>}"
OUT="${OUT_DIR}/nvidia.prom"
TMP="$(mktemp "${OUT}.XXXXXX")"
trap 'rm -f "${TMP}"' EXIT

# field  metric-suffix  scale  help
FIELDS=(
  "temperature.gpu|temperature_celsius|1|GPU core temperature."
  "utilization.gpu|utilization_ratio|0.01|Fraction of time a kernel ran on the GPU."
  "utilization.memory|memory_utilization_ratio|0.01|Fraction of time device memory was read or written."
  "utilization.encoder|encoder_utilization_ratio|0.01|NVENC utilization."
  "utilization.decoder|decoder_utilization_ratio|0.01|NVDEC utilization."
  "memory.used|memory_used_bytes|1048576|Device memory in use."
  "memory.total|memory_total_bytes|1048576|Total device memory."
  "fan.speed|fan_speed_ratio|0.01|Fan speed as a fraction of its maximum."
  "power.draw|power_draw_watts|1|Board power draw."
  "clocks.gr|graphics_clock_hz|1000000|Graphics clock."
  "clocks.mem|memory_clock_hz|1000000|Memory clock."
)

up=0
# The supported field list only changes with the driver, so it is cached in
# /run (cleared at boot) and rebuilt when the driver version changes. That
# halves the nvidia-smi runs: each one re-initialises NVML, which briefly
# delays CasparCG's GPU work on caspar1.
CACHE="/run/nvidia-textfile-collector.fields"
driver_id="$(sed -n 1p /proc/driver/nvidia/version 2>/dev/null)"
names=() ; specs=()
if [ -n "${driver_id}" ] && [ -r "${CACHE}" ] && [ "$(sed -n 1p "${CACHE}")" = "${driver_id}" ]; then
  help="$(sed 1d "${CACHE}")"
else
  help="$(timeout 20 nvidia-smi --help-query-gpu 2>/dev/null)"
  if [ -n "${help}" ] && [ -n "${driver_id}" ]; then
    printf '%s\n%s\n' "${driver_id}" "${help}" > "${CACHE}.tmp" && mv "${CACHE}.tmp" "${CACHE}"
  fi
fi
for spec in "${FIELDS[@]}"; do
  f="${spec%%|*}"
  if grep -qF "\"${f}\"" <<<"${help}"; then names+=("${f}"); specs+=("${spec}"); fi
done

{
  if [ "${#names[@]}" -gt 0 ] && \
     rows="$(timeout 20 nvidia-smi --query-gpu="index,uuid,name,driver_version,$(IFS=,; echo "${names[*]}")" \
             --format=csv,noheader,nounits 2>/dev/null)"; then
    up=1
    for spec in "${specs[@]}"; do
      IFS='|' read -r _ suffix _ text <<<"${spec}"
      echo "# HELP nvidia_gpu_${suffix} ${text}"
      echo "# TYPE nvidia_gpu_${suffix} gauge"
    done
    echo "# HELP nvidia_gpu_info GPU model and driver version, value always 1."
    echo "# TYPE nvidia_gpu_info gauge"
    while IFS= read -r row; do
      IFS=',' read -ra col <<<"${row}"
      for i in "${!col[@]}"; do col[i]="$(sed 's/^ *//; s/ *$//' <<<"${col[i]}")"; done
      idx="${col[0]}"; uuid="${col[1]}"; model="${col[2]//\"/}"; driver="${col[3]}"
      labels="gpu=\"${idx}\",uuid=\"${uuid}\""
      echo "nvidia_gpu_info{${labels},name=\"${model}\",driver_version=\"${driver}\"} 1"
      for j in "${!specs[@]}"; do
        IFS='|' read -r _ suffix scale _ <<<"${specs[j]}"
        v="${col[$((j + 4))]:-}"
        [[ "${v}" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || continue
        echo "nvidia_gpu_${suffix}{${labels}} $(awk -v v="${v}" -v s="${scale}" 'BEGIN { printf "%.6g", v * s }')"
      done
    done <<<"${rows}"
  fi
  echo "# HELP nvidia_smi_up Whether nvidia-smi answered on the last run."
  echo "# TYPE nvidia_smi_up gauge"
  echo "nvidia_smi_up ${up}"
  echo "# HELP nvidia_smi_last_run_timestamp_seconds When this file was written."
  echo "# TYPE nvidia_smi_last_run_timestamp_seconds gauge"
  echo "nvidia_smi_last_run_timestamp_seconds $(date +%s)"
} > "${TMP}"

chmod 0644 "${TMP}"
mv "${TMP}" "${OUT}"
trap - EXIT
