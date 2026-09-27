#!/usr/bin/env python3
"""Nordic Secure DFU over BLE (nRF5 SDK 17 bootloader), for flashing pixl.js OTA zips from a Mac.

Put the device in DFU mode first (screen shows "DFU Update", advertises as "pixl dfu").
Every run re-sends the init packet, which makes the bootloader reset its progress,
so the transfer always starts from byte 0. Each 4 KB object is CRC-checked and
re-sent on mismatch.

Usage:
    .venv/bin/python tools/ble_dfu.py --scan
    .venv/bin/python tools/ble_dfu.py path/to/pixjs_ota_vXXX.zip
"""

import argparse
import asyncio
import json
import struct
import sys
import zipfile
import zlib

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

DFU_SERVICE_UUID = "0000fe59-0000-1000-8000-00805f9b34fb"
CTRL_UUID = "8ec90001-f315-4f60-9fb8-838830daea50"
PKT_UUID = "8ec90002-f315-4f60-9fb8-838830daea50"

OP_CREATE = 0x01
OP_SET_PRN = 0x02
OP_CRC = 0x03
OP_EXECUTE = 0x04
OP_SELECT = 0x06
OP_RESPONSE = 0x60

OBJ_COMMAND = 0x01
OBJ_DATA = 0x02

RES_SUCCESS = 0x01
RES_EXT_ERROR = 0x0B

OBJECT_RETRIES = 3


class DfuError(Exception):
    pass


def load_package(path):
    with zipfile.ZipFile(path) as z:
        app = json.loads(z.read("manifest.json"))["manifest"]["application"]
        return z.read(app["dat_file"]), z.read(app["bin_file"])


def is_dfu_target(device, adv, name):
    local_name = adv.local_name or device.name or ""
    return local_name == name or DFU_SERVICE_UUID in [u.lower() for u in adv.service_uuids]


class Dfu:
    def __init__(self, client, packet_size):
        self.client = client
        self.packet_size = packet_size
        self.responses = asyncio.Queue()

    def on_notify(self, _sender, data):
        self.responses.put_nowait(bytes(data))

    async def request(self, payload, timeout=10.0):
        await self.client.write_gatt_char(CTRL_UUID, bytes(payload), response=True)
        opcode = payload[0]
        while True:
            data = await asyncio.wait_for(self.responses.get(), timeout)
            if len(data) < 3 or data[0] != OP_RESPONSE or data[1] != opcode:
                continue
            if data[2] == RES_SUCCESS:
                return data[3:]
            ext = f", ext error 0x{data[3]:02x}" if data[2] == RES_EXT_ERROR and len(data) > 3 else ""
            raise DfuError(f"opcode 0x{opcode:02x} failed: result 0x{data[2]:02x}{ext}")

    async def select(self, obj_type):
        max_size, offset, crc = struct.unpack("<III", await self.request([OP_SELECT, obj_type]))
        return max_size, offset, crc

    async def create(self, obj_type, size):
        await self.request(bytes([OP_CREATE, obj_type]) + struct.pack("<I", size))

    async def crc(self):
        offset, crc = struct.unpack("<II", await self.request([OP_CRC]))
        return offset, crc

    async def write_packets(self, data):
        for i in range(0, len(data), self.packet_size):
            await self.client.write_gatt_char(PKT_UUID, data[i : i + self.packet_size], response=False)

    async def send_init(self, dat):
        max_size, _, _ = await self.select(OBJ_COMMAND)
        if len(dat) > max_size:
            raise DfuError(f"init packet {len(dat)} B > max {max_size} B")
        await self.create(OBJ_COMMAND, len(dat))
        await self.request(bytes([OP_SET_PRN]) + struct.pack("<H", 0))
        await self.write_packets(dat)
        offset, crc = await self.crc()
        if offset != len(dat) or crc != zlib.crc32(dat):
            raise DfuError(f"init packet CRC mismatch (offset {offset}, crc 0x{crc:08x})")
        await self.request([OP_EXECUTE])

    async def send_firmware(self, fw):
        max_size, offset, _ = await self.select(OBJ_DATA)
        if offset != 0:
            raise DfuError(f"expected data offset 0 after init, got {offset}")

        total = len(fw)
        start = 0
        crc_done = 0
        while start < total:
            chunk = fw[start : start + max_size]
            end = start + len(chunk)
            expected_crc = zlib.crc32(chunk, crc_done)

            for attempt in range(1, OBJECT_RETRIES + 1):
                # create resets the write position to the last executed object
                await self.create(OBJ_DATA, len(chunk))
                await self.write_packets(chunk)
                offset, crc = await self.crc()
                if offset == end and crc == expected_crc:
                    break
                print(f"\n  object @{start}: mismatch (offset {offset}, crc 0x{crc:08x}), retry {attempt}")
            else:
                raise DfuError(f"object @{start} failed after {OBJECT_RETRIES} retries")

            if end < total:
                await self.request([OP_EXECUTE])
            else:
                # the last execute validates the signature and activates the image;
                # the device may reboot before the response gets through
                try:
                    await self.request([OP_EXECUTE], timeout=60.0)
                except (asyncio.TimeoutError, BleakError):
                    if self.client.is_connected:
                        raise
                    print("\n  device disconnected after final execute (it rebooted)")
            crc_done = expected_crc
            start = end
            print(f"\r  {end}/{total} bytes ({end * 100 // total}%)", end="", flush=True)
        print()


async def scan(name, timeout):
    print(f"Scanning {timeout:.0f}s ...")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for device, adv in found.values():
        mark = "  <== DFU target" if is_dfu_target(device, adv, name) else ""
        print(f"  {adv.local_name or device.name or '?':24} rssi {adv.rssi:4}  {device.address}{mark}")


async def flash(path, name, timeout):
    dat, fw = load_package(path)
    print(f"Package: init {len(dat)} B, firmware {len(fw)} B")

    print(f"Looking for '{name}' ...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, adv: is_dfu_target(d, adv, name), timeout=timeout
    )
    if device is None:
        raise DfuError(f"'{name}' not found: is the device on the DFU screen and no other app connected?")

    async with BleakClient(device, timeout=20.0) as client:
        pkt_char = client.services.get_characteristic(PKT_UUID)
        if pkt_char is None:
            raise DfuError("DFU packet characteristic not found")
        packet_size = min(pkt_char.max_write_without_response_size, 244)
        print(f"Connected {device.address}, packet size {packet_size} B")

        dfu = Dfu(client, packet_size)
        await client.start_notify(CTRL_UUID, dfu.on_notify)

        print("Sending init packet ...")
        await dfu.send_init(dat)
        print("Sending firmware ...")
        await dfu.send_firmware(fw)

    print("Done. The device validates the image and reboots into the new firmware.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("zip", nargs="?", help="pixjs_ota_vXXX.zip")
    parser.add_argument("--name", default="pixl dfu", help="advertised DFU name (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=20.0, help="scan timeout in seconds")
    parser.add_argument("--scan", action="store_true", help="only list nearby BLE devices")
    args = parser.parse_args()

    if args.scan:
        asyncio.run(scan(args.name, args.timeout))
        return
    if not args.zip:
        parser.error("zip path required (or use --scan)")

    try:
        asyncio.run(flash(args.zip, args.name, args.timeout))
    except (DfuError, asyncio.TimeoutError) as e:
        print(f"\nDFU failed: {e!r}\nSafe to re-run: the transfer restarts from 0.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
