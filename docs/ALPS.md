# Mellow Fly ALPSv6: Klipper USB Flashing & Basic Configuration

This guide covers the process of flashing the Klipper "client" firmware onto the Mellow Fly
ALPSv6 via its internal STM32F072 microcontroller over USB, and setting up the basic
configuration to use it as a native Klipper load cell probe.

The ALPS is the load cell autopa was validated against, but autopa works with **any**
Klipper [`load_cell`](https://www.klipper3d.org/Config_Reference.html#load_cell) /
[`load_cell_probe`](https://www.klipper3d.org/Config_Reference.html#load_cell_probe) — this
guide is just one concrete hardware path.

*Note: This guide assumes the ALPS has already been physically mounted to your printer's
toolhead.*

## 1. Hardware Connection & DFU Mode

The ALPS utilizes the STM32F072's built-in ROM bootloader for initial USB flashing.

1. Connect the ALPS to your host (e.g., Raspberry Pi, BTT Pi) using a USB-C data cable.
2. Locate the **BOOT (B)** and **RESET (R)** buttons on the ALPS board.
3. Press and **hold** the **BOOT** button.
4. While holding BOOT, press and **release** the **RESET** button.
5. Release the **BOOT** button.
6. Verify the board is in DFU mode by running `lsusb` in your host terminal. You should see
   a device listed as `STMicroelectronics STM Device in DFU Mode` (ID `0483:df11`).

## 2. Compiling the Klipper Firmware

On your host, navigate to your Klipper directory and configure the build for the ALPS's
specific MCU.

```bash
cd ~/klipper
make clean
make menuconfig
```

Apply the following settings in the `menuconfig` interface:

*   **Micro-controller Architecture:** `STM32`
*   **Processor model:** `STM32F072`
*   **Clock Reference:** `8 MHz crystal`
*   **Communication interface:** `USB (on PA11/PA12)`
*   **Bootloader offset:** `No bootloader`

*Important Note on Bootloader Offset:* The "No bootloader" setting is strictly a linker
offset. Because the STM32's hardware ROM DFU bootloader resides in a separate system memory
region, it does not occupy main flash space. Setting this to "No bootloader" ensures the
Klipper firmware is linked exactly at `0x08000000`, which is required for the ROM DFU to
execute it.

Exit and save (`Q`, then `Y`), then compile the firmware:

```bash
make
```

## 3. Flashing via DFU

Ensure the board is still in DFU mode (repeat Step 1 if necessary), then flash the compiled
binary. This requires `sudo` privileges.

```bash
sudo make flash FLASH_DEVICE=0483:df11
```

**Expected "Fake" Error:** At the very end of the flashing process, `dfu-util` will likely
throw an error: `dfu-util: Error during download get_status`. **This is normal and can be
safely ignored.** The `:leave` command instructs the STM32 to immediately reset and run the
new firmware. This hardware reset severs the USB connection before the host can receive the
final status packet, resulting in a timeout error. The flash itself is 100% successful.

Power cycle the ALPS (unplug and replug the USB-C cable). It should now enumerate as a
standard Klipper USB device.

## 4. Basic Klipper Configuration

Find your new serial path by running:
```bash
ls -l /dev/serial/by-id/
```
Look for the path starting with `usb-Klipper_stm32f072...`.

Add the following blocks to your `printer.cfg`. The ADS131M02 ADC requires a continuous
8 MHz master clock, which the ALPS generates internally using a hardware timer on pin `PA3`.

```ini
[mcu alps]
serial: /dev/serial/by-id/usb-Klipper_stm32f072_YOUR-ID-HERE

[static_pwm_clock alps_clk]
pin: alps:PA3
frequency: 8000000

[load_cell_probe]
sensor_type: ads131m02
cs_pin: alps:PA15
spi_bus: spi1_PB4_PB5_PB3
data_ready_pin: alps:PB6
gain: 128
pwm_clock: static_pwm_clock alps_clk
adc_channel: 0
trigger_force: 200.0
tare_time: 0.1
z_offset: 0.0
```

Save your `printer.cfg` and issue a `RESTART` command to load the new configuration.

## 5. Verification and Diagnostics

We need to verify that the ALPS is successfully streaming data to the host and that the
hardware clock is synchronized.

1. **Single Sample Read:**
Run the following command in the Klipper console:
```gcode
LOAD_CELL_READ LOAD_CELL=load_cell_probe
```
This will return the current raw ADC counts and the percentage of the sensor's capacity
currently being measured.

2. **Continuous Diagnostic Stream:**
To verify the stability of the 8 MHz clock and the SPI communication, run a 10-second
diagnostic capture:
```gcode
LOAD_CELL_DIAGNOSTIC LOAD_CELL=load_cell_probe
```
3. **Expected Results:**
   * **Sample Rate:** The output should report a measured sample rate closely matching the
     configured rate (e.g., ~488 SPS).
   * **Saturated Samples:** This should read `0`, indicating the ADC is not overflowing.

## Conclusion

At this stage, the ALPS is successfully flashed with Klipper firmware and integrated as a
secondary MCU. The 24-bit ADS131M02 ADC is actively streaming raw strain data over USB.

With basic communication verified, the sensor is ready. Next: tune the ALPS as a **Z probe**
([PROBE.md](PROBE.md)) and/or use it for **pressure-advance calibration** with autopa
([CALIBRATION.md](CALIBRATION.md)).

---

**See also:** [README](../README.md) · [Using the ALPS as a Z-probe](PROBE.md) ·
[Calibration methods](CALIBRATION.md)
