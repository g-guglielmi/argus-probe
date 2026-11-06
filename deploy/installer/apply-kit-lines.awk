# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi
#
# Sets a probe kit's lines (KEY=value, one a line, from -v kit=FILE) in DietPi's dietpi.txt and keeps
# every other line as it is: DietPi reads its own lines from that file, so it is edited, never
# replaced. The same rule as Argus's patchDietpiTxt (Add probe, the Raspberry Pi's dietpi.txt): a key
# set in the file gets the new value (its first line); one only shown as a commented example gets its
# line after that example; one missing altogether goes at the end. Line endings stay the file's own.
#
#   awk -v kit=dietpi-lines.txt -f apply-kit-lines.awk dietpi.txt dietpi.txt > new
#
# (The file is read twice: the first pass learns which keys it sets.) POSIX awk, busybox included.

BEGIN {
  n = 0
  while ((getline l < kit) > 0) {
    sub(/\r$/, "", l)
    if (l !~ /^[A-Za-z0-9_]+=/) continue
    k = substr(l, 1, index(l, "=") - 1)
    if (!(k in val)) order[++n] = k
    val[k] = l
  }
  if (n == 0) { print "apply-kit-lines: no KEY=value lines in " kit > "/dev/stderr"; exit 2 }
}

FNR == NR {
  t = $0
  if (sub(/\r$/, "", t)) cr = "\r"
  if (match(t, /^[A-Za-z0-9_]+=/)) set[substr(t, 1, RLENGTH - 1)] = 1
  next
}

{
  t = $0
  sub(/\r$/, "", t)
  if (match(t, /^[A-Za-z0-9_]+=/)) {
    k = substr(t, 1, RLENGTH - 1)
    if ((k in val) && !(k in done)) { print val[k] cr; done[k] = 1; next }
  }
  print $0
  if (match(t, /^#[ \t]*[A-Za-z0-9_]+=/)) {
    k = t
    sub(/^#[ \t]*/, "", k)
    k = substr(k, 1, index(k, "=") - 1)
    if ((k in val) && !(k in set) && !(k in done)) { print val[k] cr; done[k] = 1 }
  }
}

END {
  for (i = 1; i <= n; i++) if (!(order[i] in done)) print val[order[i]] cr
}
