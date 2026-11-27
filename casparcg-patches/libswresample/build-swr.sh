#!/usr/bin/env bash
# Rebuild Ubuntu noble's ffmpeg 7:6.1.1-3ubuntu5 with FFmpeg be8a4f80b9
# ("swr/swresample: avoid reapplication of firstpts", trac #4131) as a quilt
# patch, version 7:6.1.1-3ubuntu5+fk1. Keeps libswresample4 (+ -dev).
set -euo pipefail
cd "$(dirname "$0")"; mkdir -p out
docker run --rm -v "$PWD:/w" -e DEBIAN_FRONTEND=noninteractive ubuntu:24.04 bash -euxc '
  echo "Acquire::Retries \"8\";" > /etc/apt/apt.conf.d/80retries
  sed -i "s/^Types: deb$/Types: deb deb-src/" /etc/apt/sources.list.d/ubuntu.sources
  apt-get update -qq
  apt-get install -y -qq devscripts dpkg-dev quilt equivs >/dev/null
  mkdir -p /b && cd /b && apt-get source -qq ffmpeg=7:6.1.1-3ubuntu5
  cd ffmpeg-6.1.1
  cp /w/swr-avoid-reapplication-of-firstpts.patch debian/patches/fk-swr-avoid-reapplication-of-firstpts.patch
  echo fk-swr-avoid-reapplication-of-firstpts.patch >> debian/patches/series
  QUILT_PATCHES=debian/patches quilt push -a >/dev/null && QUILT_PATCHES=debian/patches quilt pop -a >/dev/null
  export DEBEMAIL=ops@frikanalen.no DEBFULLNAME="Frikanalen build"
  dch -v 7:6.1.1-3ubuntu5+fk1 -D noble "Backport FFmpeg be8a4f80b9 swr/swresample: avoid reapplication of firstpts (trac #4131): audio loss after first soft compensation with first_pts (CasparCG)."
  mk-build-deps -i -r -t "apt-get -y -qq --no-install-recommends" debian/control >/dev/null
  DEB_BUILD_OPTIONS="nocheck nodoc parallel=$(nproc)" dpkg-buildpackage -B -uc -us > /w/out/build.log 2>&1
  cp ../libswresample4_*fk1*.deb ../libswresample-dev_*fk1*.deb /w/out/ 2>/dev/null || true
  chown -R '"$(id -u):$(id -g)"' /w/out
'
ls -la out/
