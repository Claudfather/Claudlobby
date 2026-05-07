---
title: "Pi 5 + UHS-I SDR104 — kernel hangs from `Card stuck being busy`"
---

On Raspberry Pi 5 (kernel 6.12.x, `sdhci-brcmstb` driver), some SD cards that negotiate UHS-I SDR104 mode (200 MHz bus, 1.8 V signalling) hit intermittent kernel-level I/O hangs:

```
mmc0: Card stuck being busy! __mmc_poll_for_busy
INFO: task jbd2/mmcblk0p2-:NN blocked for more than 120 seconds.
```

System-wide effects: SSH new-connections hang (sshd can't read auth files), Docker container metadata can corrupt mid-write (an HA volume mount can reset to a default empty path), Tailscale may show the Pi "active; relay <region>" while ICMP is dead (control plane alive, data plane wedged on I/O).

**Fix** — force the controller out of every UHS-I mode and back to High-Speed at 50 MHz on 3.3 V, via the SDHCI core's `SDHCI_QUIRK2_NO_1_8_V` flag (= `0x4`):

```bash
# /boot/firmware/cmdline.txt — append to the existing single line:
sdhci.debug_quirks2=0x4
sudo reboot
```

Verify post-reboot:

```bash
$ cat /sys/kernel/debug/mmc0/ios | grep -E "clock|timing|signal"
clock:           50000000 Hz             # was 200000000
timing spec:     2 (sd high-speed)       # was 6 (sd uhs SDR104)
signal voltage:  0 (3.30 V)              # was 1 (1.80 V)

$ dmesg | grep "mmc0:.*new"
mmc0: new high speed SDXC card           # was "ultra high speed SDR104 SDXC card"
```

**Caveat:** the legacy `dtparam=sd_overclock=N` and `dtparam=sd_disable_uhs=1` from Pi 1-4 do NOT work on Pi 5 — those targeted the older `bcm2835-sdhost` driver. Pi 5's `sdhci-brcmstb` ignores them. The SDHCI core's `debug_quirks2` cmdline param is the right knob.

**Performance impact:** SD throughput drops roughly 2× (~90 → ~45 MB/s sequential). For a fleet host doing logs / state writes, irrelevant.

**Diagnostic value:** if hangs persist 24+ hours after applying this, the card itself is hardware-failing — replace. If hangs stop, the card was just incompatible with Pi 5's SDR104 negotiation. Either outcome is informative.

Originally surfaced on a Samsung SDXC card in a CanaKit Pi 5 16GB.

Sources:

- [bcm2712-rpi-5-b.dts (rpi-6.12.y)](https://github.com/raspberrypi/linux/blob/rpi-6.12.y/arch/arm64/boot/dts/broadcom/bcm2712-rpi-5-b.dts) — UHS modes enabled by default, `no-1-8-v` commented out
- [sdhci.h (kernel)](https://raw.githubusercontent.com/torvalds/linux/master/drivers/mmc/host/sdhci.h) — `SDHCI_QUIRK2_NO_1_8_V = (1<<2) = 0x4`
- RPi forum threads `t=372832`, `t=388445` — Pi 5 SDR104 stuck-busy patterns
- [diegopereiracruz/Linux-Driver-Quirks — sdhci debug quirks reference](https://github.com/diegopereiracruz/Linux-Driver-Quirks/blob/main/sdhci_debug_quirks_EN.md)
