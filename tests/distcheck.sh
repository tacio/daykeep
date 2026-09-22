#!/bin/bash
# usage: tests/distcheck.sh daykeep-VERSION.tar.gz  -- check a release tarball
# Unpacks the tarball in a scratch directory and, without help2man or
# makeinfo, builds it, runs make check, installs it into a DESTDIR,
# uninstalls it, cleans it and packs it again.  Each step must work, the
# uninstall must leave no files behind, make clean must leave exactly the
# files that were unpacked, and the second tarball must hold the same files.
#
# Copyright (C) 2026 Tacio Medeiros
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

set -e
tarball=$(realpath "${1:?usage: $0 TARBALL}")
name=$(basename "$tarball" .tar.gz)

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
cd "$scratch"

fail() { echo "distcheck: $*" >&2; exit 1; }

# the doc tools are replaced by 'false': a rule that needs them fails
m() { make -s HELP2MAN=false MAKEINFO=false "$@"; }

tar xzf "$tarball"
[ -d "$name" ] || fail "$tarball does not unpack into $name/"
cd "$name"
find . | sort > "$scratch/unpacked"

m all
m check >/dev/null || { m check; fail "make check failed"; }

inst=$scratch/inst
m install DESTDIR="$inst"
for f in bin/daykeep share/man/man1/daykeep.1 share/info/daykeep.info \
         share/bash-completion/completions/daykeep; do
	[ -f "$inst/usr/local/$f" ] || fail "make install did not install $f"
done
"$inst/usr/local/bin/daykeep" --version | grep -q "^daykeep " ||
	fail "the installed daykeep does not run"
grep -q '^\.TH DAYKEEP' "$inst/usr/local/share/man/man1/daykeep.1" ||
	fail "the installed man page has no .TH line"

m uninstall DESTDIR="$inst"
left=$(find "$inst" -type f ! -path "$inst/usr/local/share/info/dir")
[ -z "$left" ] || fail "files left after make uninstall:"$'\n'"$left"

m clean
find . | sort > "$scratch/cleaned"
diff "$scratch/unpacked" "$scratch/cleaned" >&2 ||
	fail "make clean does not restore the unpacked tree"

m dist >/dev/null
diff <(tar tzf "$tarball" | sort) <(tar tzf "$name.tar.gz" | sort) >&2 ||
	fail "make dist in the unpacked tree packs different files"

echo "$name.tar.gz: build, check, install, uninstall, clean and dist all ok"
