Name:           klipper-connect
Version:        %{_version}
Release:        1%{?dist}
Summary:        Prusa Connect bridge for Klipper printers
License:        MIT
URL:            https://github.com/amd989/klipper-connect
Source0:        %{name}-%{version}.tar.gz

# The Python code and its dependencies live in /usr/lib/klipper-connect as
# plain folders (see packaging/build-lib.sh). Keep rpm's helpers away from
# them: no debuginfo, no shebang rewriting, and the bundled packages provide
# nothing to the rest of the system.
%global debug_package %{nil}
%global __brp_mangle_shebangs_exclude_from ^%{_prefix}/lib/klipper-connect/
%global __provides_exclude_from ^%{_prefix}/lib/klipper-connect/

BuildRequires:  python3 >= 3.9
BuildRequires:  gcc
BuildRequires:  systemd-rpm-macros

Requires:       python3 >= 3.9
Requires:       systemd
Recommends:     alejandro-md-release

%description
Runs next to Moonraker and makes a Klipper printer show up in Prusa Connect:
status, telemetry, files, starting and stopping prints, camera snapshots.
Supports the MK3S and the MINI.

One bridge per printer runs as the klipper-connect@NAME systemd service. Set
one up with `sudo klipper-connect-setup`; nothing is started on install.

%prep
%setup -q

%build

%install
sh packaging/build-lib.sh %{buildroot}%{_prefix}/lib/klipper-connect
install -D -m 0755 packaging/klipper-connect %{buildroot}%{_bindir}/klipper-connect
install -D -m 0755 packaging/klipper-connect-setup %{buildroot}%{_sbindir}/klipper-connect-setup
install -D -m 0644 packaging/klipper-connect@.service %{buildroot}%{_unitdir}/klipper-connect@.service
install -D -m 0644 klipper_connect.conf.example %{buildroot}%{_datadir}/klipper-connect/klipper_connect.conf.example

%files
%license LICENSE
%doc README.md
%{_bindir}/klipper-connect
%{_sbindir}/klipper-connect-setup
%{_prefix}/lib/klipper-connect
%{_unitdir}/klipper-connect@.service
%{_datadir}/klipper-connect

%post
systemctl daemon-reload >/dev/null 2>&1 || :
if [ "$1" -eq 1 ]; then
    echo "klipper-connect installed. Set up a printer with: sudo klipper-connect-setup"
fi

%preun
if [ "$1" -eq 0 ]; then
    systemctl stop 'klipper-connect@*.service' >/dev/null 2>&1 || :
fi
# Written by %%post, so not in the package's file list
find %{_prefix}/lib/klipper-connect -name __pycache__ -type d -prune \
    -exec rm -rf {} + 2>/dev/null || :

%postun
systemctl daemon-reload >/dev/null 2>&1 || :
if [ "$1" -ge 1 ]; then
    # Upgrade: restart the bridges that were running
    systemctl try-restart 'klipper-connect@*.service' >/dev/null 2>&1 || :
fi

%posttrans
# Byte-compile for whichever Python 3 this system has. In %%posttrans so an
# upgrade's old %%preun can't run after it.
python3 -m compileall -q %{_prefix}/lib/klipper-connect >/dev/null 2>&1 || :

%changelog
