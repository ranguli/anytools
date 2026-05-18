#!/usr/bin/env python3
"""
nand_dump_host.py — drive OpenOCD over telnet to orchestrate a raw NAND dump.

Loads the RAM-resident nand_dump_stub via SWD, iteratively reads NAND pages
into an SRAM buffer, and retrieves the data via OpenOCD's dump_image.

"""

import socket
import time
import sys
import os
import re
import argparse
import signal

NAND_TOTAL    = 0x08000000   # 128 MiB
TEST_START    = 0x02f40000   # calibration region
TEST_END      = 0x03000000
MB_ADDR       = 0x20000000   # mailbox base
MB_STATUS     = MB_ADDR + 0x00
MB_NAND_ADDR  = MB_ADDR + 0x04
MB_BYTES_RD   = MB_ADDR + 0x08
MB_ERROR      = MB_ADDR + 0x0C
MB_CHUNK_SIZE = MB_ADDR + 0x10
BUF_ADDR      = 0x20001000   # data buffer in SRAM
SP_INIT       = 0x20017F00
PC_INIT       = 0x20000100
STUB_PATH     = "tools/ram_tests/nand_dump_stub.bin"

STATUS_RUNNING = 0xDEAD0000
STATUS_DONE    = 0xDEAD0001
STATUS_ERROR   = 0xDEAD0002


class OpenOCDError(Exception):
    pass


class OpenOCD:
    """Minimal OpenOCD telnet client for SWD memory and execution control."""

    def __init__(self, host="localhost", port=4444):
        self.host = host
        self.port = port
        self.sock = None
        self._connect()

    def _connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        try:
            self.sock.connect((self.host, self.port))
        except (ConnectionRefusedError, socket.timeout) as e:
            raise OpenOCDError(
                f"Cannot connect to OpenOCD at {self.host}:{self.port}. "
                f"Is it running? ({e})"
            ) from e
        # drain banner
        time.sleep(0.1)
        self._read_until_prompt()

    def _read_until_prompt(self):
        """Read all available data until the '> ' prompt."""
        data = b""
        deadline = time.time() + 5.0
        while True:
            try:
                self.sock.settimeout(max(0.1, deadline - time.time()))
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise OpenOCDError("OpenOCD connection closed")
                data += chunk
                if data.rstrip().endswith(b">"):
                    break
            except socket.timeout:
                break
        return data.decode("utf-8", errors="replace")

    def _send(self, cmd):
        """Send a command and return the response text."""
        self.sock.sendall((cmd + "\n").encode())
        return self._read_until_prompt()

    def halt(self):
        r = self._send("halt")
        if "target halted" not in r.lower() and "target not halted" not in r.lower():
            # accept either — target may already be halted
            pass

    def resume(self):
        self._send("resume")

    def mww(self, addr, val):
        self._send(f"mww {addr:#x} {val:#x}")

    def mrw(self, addr):
        """Read 32-bit word. Works while target is running (AHB-AP)."""
        resp = self._send(f"mrw {addr:#x}")
        # Parse: '0x20000000: dead0001' or just hex value
        m = re.search(r"(?:0x)?[0-9a-fA-F]+:\s*([0-9a-fA-F]+)", resp)
        if m:
            return int(m.group(1), 16)
        for line in resp.split("\n"):
            line = line.strip()
            if re.match(r"^[0-9a-fA-F]{8}$", line):
                return int(line, 16)
        raise OpenOCDError(f"cannot parse mrw response: {resp!r}")

    def reg(self, name, val):
        self._send(f"reg {name} {val:#x}")

    def load_image(self, path, addr):
        r = self._send(f"load_image {path} {addr:#x} bin")
        if "error" in r.lower():
            raise OpenOCDError(f"load_image failed: {r}")

    def dump_image(self, path, addr, size):
        r = self._send(f"dump_image {path} {addr:#x} {size}")
        if "error" in r.lower() or "dumped" not in r.lower():
            raise OpenOCDError(f"dump_image failed: {r}")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(
        description="Dump TC58CVG0S3HRAIJ serial NAND via RAM-resident SWD stub"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help=f"Dump calibration region only ({TEST_START:#010x}–{TEST_END:#010x})",
    )
    parser.add_argument(
        "--start",
        default="0x00000000",
        help="NAND byte address to start at (default: 0x00000000)",
    )
    parser.add_argument(
        "--end",
        default="0x08000000",
        help="NAND byte address to stop at (default: 0x08000000 = 128 MiB)",
    )
    parser.add_argument(
        "--output", "-o",
        default="nand_physical.bin",
        help="Output file (default: nand_physical.bin)",
    )
    parser.add_argument(
        "--chunk",
        type=lambda x: int(x, 0),
        default=61440,
        help="Bytes per SWD transfer (default: 61440 = 60 KiB)",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=4444,
        help="OpenOCD telnet port (default: 4444)",
    )
    parser.add_argument(
        "--stub",
        default=STUB_PATH,
        help="Path to nand_dump_stub.bin",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for stub to finish one chunk (default: 15)",
    )
    args = parser.parse_args()

    # resolve range 

    if args.test:
        start = TEST_START
        end = TEST_END
        print(f"Test mode: dumping calibration region {start:#010x}–{end:#010x}")
    else:
        start = int(args.start, 16)
        end = int(args.end, 16)

    if end > NAND_TOTAL:
        print(f"Clamping end from {end:#x} to {NAND_TOTAL:#x} (device size)")
        end = NAND_TOTAL
    if start >= end:
        print(f"Error: start ({start:#x}) >= end ({end:#x})")
        sys.exit(1)

    # page-align the range
    page_mask = 2047
    start = start & ~page_mask
    end = (end + page_mask) & ~page_mask
    total_bytes = end - start

    # verify stub binary exists

    if not os.path.isfile(args.stub):
        print(f"Error: stub not found at '{args.stub}'. Run: make -C tools/ram_tests")
        sys.exit(1)

    # connect

    print(f"Connecting to OpenOCD on port {args.port} ...", end=" ", flush=True)
    ocd = OpenOCD(port=args.port)
    print("ok")

    # dump loop 

    addr = start
    chunk = args.chunk
    chunk = chunk & ~page_mask  # make chunk page-aligned
    if chunk < 2048:
        chunk = 2048
    if chunk > 61440:
        chunk = 61440  # buffer is 60 KiB

    print(f"NAND range: {start:#010x} – {end:#010x} ({total_bytes} bytes)")
    print(f"Chunk size: {chunk} bytes ({chunk // 2048} pages)")
    print(f"Output:     {args.output}")
    print(f"Stub:       {args.stub}")
    print()

    with open(args.output, "wb") as outf:
        nchunks = 0
        t_start = time.time()

        while addr < end:
            this_chunk = min(chunk, end - addr)

            # load and run stub

            ocd.halt()
            ocd.mww(MB_NAND_ADDR, addr)
            ocd.mww(MB_CHUNK_SIZE, this_chunk)
            ocd.mww(MB_STATUS, 0)  # clear status
            ocd.load_image(args.stub, PC_INIT)
            ocd.reg("msp", SP_INIT)
            ocd.reg("pc", PC_INIT)
            ocd.resume()

            # poll status (non-intrusive mrw while running)

            t0 = time.time()
            status = 0
            while time.time() - t0 < args.timeout:
                try:
                    status = ocd.mrw(MB_STATUS)
                except OpenOCDError:
                    time.sleep(0.1)
                    continue
                if status in (STATUS_DONE, STATUS_ERROR):
                    break
                time.sleep(0.05)
            else:
                print(f"[{addr:#010x}] TIMEOUT after {args.timeout:.0f}s")
                ocd.halt()
                break

            if status == STATUS_ERROR:
                ocd.halt()
                err_code = ocd.mrw(MB_ERROR)
                nread = ocd.mrw(MB_BYTES_RD)
                print(f"[{addr:#010x}] ERROR: code={err_code:#x}, "
                      f"partial={nread} bytes")
                if nread > 0:
                    tmp = f"/tmp/_nand_err_{os.getpid()}.bin"
                    ocd.dump_image(tmp, BUF_ADDR, nread)
                    with open(tmp, "rb") as f:
                        outf.write(f.read())
                    os.unlink(tmp)
                break

            # read back buffer

            ocd.halt()
            nread = ocd.mrw(MB_BYTES_RD)

            if nread == 0:
                print(f"[{addr:#010x}] 0 bytes — NAND end or unreadable")
                break

            tmp = f"/tmp/_nand_chunk_{os.getpid()}.bin"
            ocd.dump_image(tmp, BUF_ADDR, nread)

            with open(tmp, "rb") as f:
                data = f.read()
                outf.write(data)
            os.unlink(tmp)

            if len(data) != nread:
                print(f"  WARNING: expected {nread} bytes, got {len(data)}")

            addr += nread
            nchunks += 1
            pct = (addr - start) * 100 // total_bytes
            elapsed = time.time() - t_start
            rate = (addr - start) / elapsed / 1024 if elapsed > 0 else 0

            print(f"[{addr:#010x}] {pct:3d}%  {nread:6d} bytes  "
                  f"{rate:6.1f} KiB/s  [{nchunks} chunks]",
                  flush=True)

    # suppers ready 

    ocd.halt()
    ocd.close()

    total = addr - start
    elapsed = time.time() - t_start
    rate = total / elapsed / 1024 if elapsed > 0 else 0
    print(f"\nDone: {total} bytes in {elapsed:.1f}s ({rate:.1f} KiB/s)")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
