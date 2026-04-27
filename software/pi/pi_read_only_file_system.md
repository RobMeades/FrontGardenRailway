These notes taken from:

https://www.dzombak.com/blog/2024/03/running-a-raspberry-pi-with-a-read-only-root-filesystem/

All credit goes to Chris Dzombak and those who helped him compile the guide.  The process below worked for me on a Raspberry Pi Zero W (1.1 and 2) running Raspberry Pi OS 13 (Trixie), headless version over a Wifi network (hence disabling of Wifi is not included below).

- Run an update and reboot:

  ```
  sudo apt update && sudo apt upgrade
  sudo apt autoremove --purge
  sudo reboot now
  ```

- Disable the swap file with:

  ```
  sudo swapoff -a
  sudo systemctl mask swap.target
  ```

- Reboot and verify that the swap file has gone by running:

  `free -m`

  ...to get something like:

  ```
                  total        used        free      shared  buff/cache   available
  Mem:            430          33         333           6          63         342
  Swap:             0           0           0
  ```

  ...where the numbers on the `Swap:` line are all zero.

- Similarly:

  `sudo nano /boot/firmware/cmdline.txt`

  ...and append `fsck.mode=skip noswap` to the line, so it will look something like:

  `console=serial0,115200 console=tty1 root=PARTUUID=76b4450a-02 rootfstype=ext4 elevator=deadline fsck.repair=yes rootwait fsck.mode=skip noswap`

- Disable automatic periodic re-building of the `manpages` cache with:

  `sudo rm /var/lib/man-db/auto-update`

- Disable daily software updates with:

  ```
  sudo systemctl mask apt-daily-upgrade
  sudo systemctl mask apt-daily
  sudo systemctl disable apt-daily-upgrade.timer
  sudo systemctl disable apt-daily.timer
  ```

- Disable the `avahi` daemon, which is connected somehow with the support of `.local` domains, with:

  ```
  sudo apt remove --purge avahi-daemon
  sudo apt autoremove --purge
  ```

- Disable the evil modem manager, and also Bluetooth, with:

  ```
  sudo apt remove --purge modemmanager
  sudo apt autoremove --purge
  sudo systemctl disable bluetooth.service
  ```

- Similarly:

  `sudo nano /boot/firmware/config.txt`

  ...and add, near the top:

  ```
  # Disable bt
  dtoverlay=disable-bt
  ```

  ...then reboot.

- You can now remove some additional software:

  ```
  sudo apt remove --purge bluez
  sudo apt autoremove --purge
  ```

- Disable `cloud-init` first boot setup tasks with:

  ```
  sudo systemctl mask cloud-init.service
  sudo systemctl mask cloud-init.target
  ```

- `systemd-timesyncd` apparently won't work with a read-only file system, so switch to `ntp` with:

  ```
  sudo systemctl disable systemd-timesyncd.service
  sudo apt install ntpsec
  ```

- Edit `sudo nano /etc/ntpsec/ntp.conf` and put the `driftfile` in `/var/tmp/ntp.drift` (which we will put into RAM later).

- Enable `ntp` with:

  `sudo systemctl enable ntpsec`

- `sudo systemctl edit ntp` and paste in the following lines:

  ```
  [Service]
  PrivateTmp=false
  ```

  Those will be the only lines in that file.

- In the next step we will move `resolv.conf` to `/var/run`, which will allow NetworkManager update it when needed, but means it will be deleted every time the system shuts down. By default, though, NetworkManager won’t touch `/etc/resolv.conf` if it is a symlink.  To allow NetworkManager to recreate `resolv.conf` when the system restarts, `sudo nano /etc/NetworkManager/NetworkManager.conf` and add `rc-manager=file` under the [main] section, e.g.:

  ```
  [main]
  plugins=ifupdown,keyfile
  rc-manager=file
  
  [ifupdown]
  managed=false
  ```

- Move some files that need to remain writable to `/var/run` (which is already a `tmpfs`) and create symlinks from their original locations:

  ```
  sudo mv /etc/resolv.conf /var/run/resolv.conf && sudo ln -s /var/run/resolv.conf /etc/resolv.conf
  sudo rm -rf /var/lib/dhcp && sudo ln -s /var/run /var/lib/dhcp
  sudo rm -rf /var/lib/NetworkManager && sudo ln -s /var/run /var/lib/NetworkManager
  ```

- Move the existing `systemd` random-seed file to a path that we will put on a `tmpfs` and link to it from the original location:

  `sudo mv /var/lib/systemd/random-seed /tmp/systemd-random-seed && sudo ln -s /tmp/systemd-random-seed /var/lib/systemd/random-seed`

- To create this file in the `/tmp` folder at boot before starting the random-seed service, edit the file service file to add an `ExecStartPre` command by running:

  `sudo systemctl edit systemd-random-seed.service`

  ...and pasting these lines in:

  ```
  [Service]
  ExecStartPre=/bin/echo "" >/tmp/systemd-random-seed
  ```

- Disable `systemd-rfkill` with:

  ```
  sudo systemctl disable systemd-rfkill.service
  sudo systemctl mask systemd-rfkill.socket
  ```

- Disable daily `apt` and `mandb` tasks:

  ```
  sudo systemctl mask man-db.timer
  sudo systemctl mask apt-daily.timer
  sudo systemctl mask apt-daily-upgrade.timer
  ```

- To move temporary folders to `tmpfs`, `sudo nano /etc/fstab` and append these lines (ensuring there are NO leading spaces):

  ```
  tmpfs  /tmp      tmpfs  defaults,noatime,nosuid,nodev   0  0
  tmpfs  /var/tmp  tmpfs  defaults,noatime,nosuid,nodev   0  0
  ```

- To move some spool folders to `tmpfs`, `sudo nano /etc/fstab` and append these lines (ensuring there are NO leading spaces):

  ```
  tmpfs  /var/spool/mail  tmpfs  defaults,noatime,nosuid,nodev,noexec,size=25m  0  0
  tmpfs  /var/spool/rsyslog  tmpfs  defaults,noatime,nosuid,nodev,noexec,size=25m  0  0
  ```

- Add another `tmpfs` to `/etc/fstab` for the `/var/log` folder:

 `tmpfs  /var/log  tmpfs  defaults,noatime,nosuid,nodev,noexec,size=50m  0  0`

- When storing `/var/log` in RAM, unless you have disabled `journald`, you need to limit the amount of space `journald` is allowed to use. To do that, `sudo nano /etc/systemd/journald.conf` and uncomment the `SystemMaxUse=...` line (if necessary) then set it to half of your `/var/log` `tmpfs` size, or maybe a little less:

  ```
  [Journal]
  # <output snipped>
  SystemMaxUse=25M
  # <output snipped>
  ```

- `logrotate` stores some state in `/var/lib/logrotate` and may not work if it cannot update that folder; move `logrotate` state to `tmpfs` by adding this line to `/etc/fstab`:

  `tmpfs  /var/lib/logrotate  tmpfs  defaults,noatime,nosuid,nodev,noexec,size=1m,mode=0755  0  0`

- `sudo` stores some state in `/var/lib/sudo` which should be writable. Move `sudo` state to `tmpfs` by adding this line to `/etc/fstab`:

  `tmpfs  /var/lib/sudo  tmpfs  defaults,noatime,nosuid,nodev,noexec,size=1m,mode=0700  0  0`

- Reboot and check that all comes up OK, we are about to do the read-only thing; you'll be able to switch back to read/write easily enough though, don't worry.

- `sudo nano /boot/firmware/cmdline.txt` and append `ro` to the line, e.g.:

  `console=serial0,115200 console=tty1 root=PARTUUID=76b4450a-02 rootfstype=ext4 elevator=deadline fsck.repair=yes rootwait fsck.mode=skip noswap ro`

- `sudo nano /etc/fstab` and change the lines that refer to your SD card. In column 4, after the word defaults (without adding any whitespace), add the `,ro` flag to both SD card mounts and, if it is not there already, add the `,noatime` option to the `/` mount, e.g.:

  ```
  proc            /proc           proc    defaults          0       0

  PARTUUID=76b4450a-01  /boot/firmware  vfat    defaults,ro          0       2
  PARTUUID=76b4450a-02  /               ext4    defaults,noatime,ro  0       1
  ```

- To allow you to switch between read-only (bash command `ro`) and read-write (bash command `rw`) mode, `sudo nano /etc/bash.bashrc` and add the following lines to the end:

```
  set_bash_prompt(){
      fs_mode=$(mount | sed -n -e "s/^\/dev\/.* on \/ .*(\(r[w|o]\).*/\1/p")
      PS1='\[\033[01;32m\]\u@\h${fs_mode:+($fs_mode)}\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '
  }
  PROMPT_COMMAND=set_bash_prompt

  alias ro='sudo mount -o remount,ro / ; sudo mount -o remount,ro /boot/firmware'
  alias rw='sudo mount -o remount,rw / ; sudo mount -o remount,rw /boot/firmware'
```

- To use `bash_logout` to switch to read-only mode when you log out, `sudo nano /etc/bash.bash_logout` (creating the file if necessary) and make sure the file includes the line:

  `sudo mount -o remount,ro / ; sudo mount -o remount,ro /boot/firmware`

- Reboot, verify with mount and check `journalctl` for issues:

  `sudo reboot now`

  ...SSH back in when the system comes back up and:

  `mount`

  ...then verify that the SD card partitions (e.g. `/dev/mmc*`) are mounted `ro`, and:

  `sudo journalctl -b 0`


 ...then scroll through and look for any issues.  When looking for issues, you will undoubtedly see some errors from various processes.  You will want to investigate those.  Start by checking "is this actually broken?".  Often there will be messages from e.g. `avahi-daemon` or `snapd` that are unhappy they cannot go about their business normally on a read-only filesystem.  But as long as that software is still working for your purposes, you can safely ignore their complaints.

