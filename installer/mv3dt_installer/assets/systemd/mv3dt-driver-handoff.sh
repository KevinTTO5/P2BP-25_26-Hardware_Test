#!/usr/bin/env bash
set -euo pipefail

runfile=$1
status_file=$2
log_file=$3
boot_id_file=$4
status_tmp="${status_file}.tmp"
display_manager=
finished=0

write_status() {
  printf '%s\n' "$1" >"${status_tmp}"
  chmod 0600 "${status_tmp}"
  mv -f "${status_tmp}" "${status_file}"
}

restore_desktop() {
  if [[ -n "${display_manager}" ]]; then
    systemctl start "${display_manager}"
  else
    systemctl start display-manager.service
  fi
}

record_failure() {
  reason=$1
  write_status "${reason}"
  if ! restore_desktop; then
    write_status "${reason}:desktop-restore"
  fi
}

handle_unexpected_exit() {
  exit_code=$?
  if ((finished == 0)); then
    set +e
    record_failure "failed:worker:${exit_code}"
  fi
}

trap handle_unexpected_exit EXIT

write_status running
: >"${log_file}"
chmod 0600 "${log_file}"

for unit in gdm3 gdm lightdm sddm; do
  if systemctl is-active --quiet "${unit}"; then
    display_manager=${unit}
    if ! systemctl stop "${unit}"; then
      printf 'Could not stop display manager %s\n' "${unit}" >>"${log_file}"
      record_failure failed:display-manager
      finished=1
      exit 1
    fi
    break
  fi
done

pkill -9 Xorg 2>/dev/null || true
chmod 0755 "${runfile}"

set +e
"${runfile}" --silent --no-cc-version-check 2>&1 | tee -a "${log_file}"
runfile_rc=${PIPESTATUS[0]}
set -e

if ((runfile_rc != 0)); then
  record_failure "failed:runfile:${runfile_rc}"
  finished=1
  exit "${runfile_rc}"
fi

boot_id=$(< "${boot_id_file}")
write_status "succeeded:${boot_id}"
sync
if ! systemctl --no-block reboot; then
  record_failure failed:reboot
  finished=1
  exit 1
fi
finished=1
