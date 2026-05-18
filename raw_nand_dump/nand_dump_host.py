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
import tempfile
import json

NAND_TOTAL    = 0x08000000   # 128 MiB
TEST_START    = 0x02f40000   # calibration region
TEST_END      = 0x03000000
MB_ADDR       = 0x20000000   # mailbox base
MB_STATUS     = MB_ADDR + 0x00
MB_NAND_ADDR  = MB_ADDR + 0x04
MB_BYTES_RD   = MB_ADDR + 0x08
MB_ERROR      = MB_ADDR + 0x0C
MB_CHUNK_SIZE = MB_ADDR + 0x10
MB_LAST_STAT  = MB_ADDR + 0x14
MB_RETRIES    = MB_ADDR + 0x18
MB_FLAGS      = MB_ADDR + 0x1C
BUF_ADDR      = 0x20001000   # data buffer in SRAM
SP_INIT       = 0x20017F00
PC_INIT       = 0x20000100
STUB_PATH     = "nand_dump_stub.bin"

STATUS_RUNNING = 0xDEAD0000
STATUS_DONE    = 0xDEAD0001
STATUS_ERROR   = 0xDEAD0002

ERR_CHIP_DETECT = 1
ERR_STATUS_TIMEOUT = 2
ERR_CACHE_READ = 3

FLAG_DEBUG_PAGE = 1
DEBUG_VARIANT_BYTES = 32

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

    def _read_until_prompt(self, timeout=5.0):
        """Read all available data until the '> ' prompt."""
        data = b""
        deadline = time.time() + timeout
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

    def _send(self, cmd, timeout=5.0):
        """Send a command and return the response text."""
        self.sock.sendall((cmd + "\n").encode())
        return self._read_until_prompt(timeout=timeout)

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
        """Read 32-bit word. Works while halted."""
        resp = self._send(f"mrw {addr:#x}")
        # OpenOCD responds in two known formats:
        #   A: '0x20000000: dead0001\r\n> '
        #   B: 'mrw 0x20000000\r\n\x000xdead0002\r\n> '
        m = re.search(rf"{addr:#x}:\s*([0-9a-fA-F]+)", resp)
        if m:
            return int(m.group(1), 16)
        # Format B: find all 0x-prefixed hex values, take last
        matches = re.findall(r"0x([0-9a-fA-F]+)", resp)
        if matches:
            return int(matches[-1], 16)
        raise OpenOCDError(f"cannot parse mrw response: {resp!r}")

    def reg(self, name, val):
        self._send(f"reg {name} {val:#x}")

    def load_image(self, path, addr):
        r = self._send(f"load_image {path} {addr:#x} bin")
        if "error" in r.lower():
            raise OpenOCDError(f"load_image failed: {r}")

    def dump_image(self, path, addr, size):
        r = self._send(f"dump_image {path} {addr:#x} {size}", timeout=60.0)
        if "error" in r.lower():
            raise OpenOCDError(f"dump_image failed: {r}")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


def format_hex(data):
    return " ".join(f"{b:02x}" for b in data)


def dump_debug_page(ocd, addr, stub_path, logical_path):
    ocd.halt()
    ocd.mww(MB_NAND_ADDR, addr)
    ocd.mww(MB_CHUNK_SIZE, 0)
    ocd.mww(MB_FLAGS, FLAG_DEBUG_PAGE)
    ocd.mww(MB_STATUS, 0)
    ocd.load_image(stub_path, PC_INIT)
    ocd.reg("msp", SP_INIT)
    ocd.reg("pc", PC_INIT)
    ocd.resume()

    time.sleep(0.5)
    ocd.halt()

    status = ocd.mrw(MB_STATUS)
    if status != STATUS_DONE:
        err_code = ocd.mrw(MB_ERROR)
        last_stat = ocd.mrw(MB_LAST_STAT) & 0xFF
        retries = ocd.mrw(MB_RETRIES)
        raise OpenOCDError(
            f"debug page failed: status={status:#x} err={err_code:#x} "
            f"last_stat=0x{last_stat:02x} retries={retries}"
        )

    nread = ocd.mrw(MB_BYTES_RD)
    tmp = os.path.join(tempfile.gettempdir(), f"nand_debug_{os.getpid()}.bin")
    ocd.dump_image(tmp, BUF_ADDR, nread)
    data = open(tmp, "rb").read()
    os.unlink(tmp)

    print(f"Debug page @ {addr:#010x}")
    labels = [
        "single_phase",
        "single_legacy",
        "dual_pa_pb",
        "dual_pb_pa",
    ]
    for idx, label in enumerate(labels):
        start = idx * DEBUG_VARIANT_BYTES
        end = start + DEBUG_VARIANT_BYTES
        print(f"{label:14} {format_hex(data[start:end])}")

    logical_file = logical_path
    if logical_file and os.path.isfile(logical_file):
        with open(logical_file, "rb") as f:
            f.seek(addr)
            same = f.read(DEBUG_VARIANT_BYTES)
            f.seek(0x000BB080)
            repeated = f.read(DEBUG_VARIANT_BYTES)
        print(f"logical_same    {format_hex(same)}")
        print(f"logical_000bb080 {format_hex(repeated)}")


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
    parser.add_argument(
        "--debug-page",
        action="store_true",
        help="Read one page through multiple payload decoders and print samples",
    )
    parser.add_argument(
        "--logical",
        default="../../../dumps/nand_factory_reset.bin",
        help="Logical dump path used for debug-page reference output",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="If the output file already exists, append to it and continue "
             "from where it left off instead of restarting from --start",
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

    # resume support
    #
    # Default behaviour truncates the output and dumps from `start`.
    # --resume continues a run THIS script started for the SAME range,
    # tracked via a sidecar so it can never mistake a foreign file
    # (e.g. an old test-region dump) for a 0x0-based prefix.
    resume_meta = args.output + ".resume"
    out_mode = "wb"
    addr_start = start

    if args.resume:
        if not os.path.isfile(args.output):
            print(f"--resume: no existing '{args.output}'; starting fresh")
        elif not os.path.isfile(resume_meta):
            print(f"ERROR: --resume given but '{resume_meta}' is missing. "
                  f"'{args.output}' was not produced by a resumable run of "
                  f"this script (it may be an old test dump). Move it aside "
                  f"and run without --resume.")
            sys.exit(1)
        else:
            try:
                meta = json.load(open(resume_meta))
            except (OSError, ValueError) as e:
                print(f"ERROR: cannot read {resume_meta}: {e}")
                sys.exit(1)
            if meta.get("start") != start or meta.get("end") != end:
                print(f"ERROR: --resume range mismatch. Sidecar was "
                      f"start={meta.get('start'):#x} end={meta.get('end'):#x}, "
                      f"this run is start={start:#x} end={end:#x}. Refusing to "
                      f"append mismatched data. Start fresh or match the range.")
                sys.exit(1)
            have = os.path.getsize(args.output)
            whole = have & ~page_mask          # drop any torn trailing page
            if whole != have:
                with open(args.output, "r+b") as _f:
                    _f.truncate(whole)
                print(f"Resume: trimmed {have - whole} partial-page bytes")
            addr_start = start + whole
            if addr_start >= end:
                print(f"Resume: output already covers the range "
                      f"({whole} bytes from {start:#x}); nothing to do")
                return
            out_mode = "ab"
            print(f"Resume: {whole} bytes present, continuing at "
                  f"{addr_start:#010x} ({end - addr_start} bytes left)")

    # record range provenance so a later --resume can trust this file
    if out_mode == "wb":
        try:
            json.dump({"start": start, "end": end}, open(resume_meta, "w"))
        except OSError as e:
            print(f"Warning: could not write {resume_meta}: {e}")

    # verify stub binary exists

    if not os.path.isfile(args.stub):
        print("Error: stub not found at "
              f"'{args.stub}'. Run: make -C tools/anytools/raw_nand_dump")
        sys.exit(1)

    # connect

    print(f"Connecting to OpenOCD on port {args.port} ...", end=" ", flush=True)
    ocd = OpenOCD(port=args.port)
    print("ok")

    # dump loop 

    addr = addr_start
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

    if args.debug_page:
        dump_debug_page(ocd, start, args.stub, args.logical)
        ocd.halt()
        ocd.close()
        return

    with open(args.output, out_mode) as outf:
        nchunks = 0
        t_start = time.time()

        while addr < end:
            this_chunk = min(chunk, end - addr)

            # load and run stub

            ocd.halt()
            ocd.mww(MB_NAND_ADDR, addr)
            ocd.mww(MB_CHUNK_SIZE, this_chunk)
            ocd.mww(MB_FLAGS, 0)
            ocd.mww(MB_STATUS, 0)  # clear status
            ocd.load_image(args.stub, PC_INIT)
            ocd.reg("msp", SP_INIT)
            ocd.reg("pc", PC_INIT)
            ocd.resume()

            # wait for stub to finish
            #
            # Poll up to args.timeout for real, via halt/check/resume
            # (mrw-while-running is unreliable on some CMSIS-DAP
            # adapters). The message must reflect the real wait.

            deadline = time.time() + args.timeout
            poll = 0.2
            status = None
            while True:
                time.sleep(poll)
                ocd.halt()
                status = ocd.mrw(MB_STATUS)
                if status in (STATUS_DONE, STATUS_ERROR):
                    break
                if time.time() >= deadline:
                    break
                ocd.resume()

            if status not in (STATUS_DONE, STATUS_ERROR):
                waited = args.timeout
                print(f"[{addr:#010x}] TIMEOUT: stub still {status:#010x} "
                      f"after {waited:.0f}s of polling. The stub is hung or "
                      f"slow, not finishing this chunk.")
                break

            if status == STATUS_ERROR:
                err_code = ocd.mrw(MB_ERROR)
                nread = ocd.mrw(MB_BYTES_RD)
                last_stat = ocd.mrw(MB_LAST_STAT) & 0xFF
                retries = ocd.mrw(MB_RETRIES)
                if err_code == ERR_CHIP_DETECT:
                    err_name = "chip detect failed"
                elif err_code == ERR_STATUS_TIMEOUT:
                    err_name = "status poll timeout"
                elif err_code == ERR_CACHE_READ:
                    err_name = "cache read failed"
                else:
                    err_name = "unknown"
                print(f"[{addr:#010x}] ERROR: code={err_code:#x} ({err_name}), "
                      f"partial={nread} bytes, "
                      f"status=0x{last_stat:02x}, retries={retries}")
                # Do not trust buffer content on error
                break

            # read back buffer

            ocd.halt()
            nread = ocd.mrw(MB_BYTES_RD)
            last_stat = ocd.mrw(MB_LAST_STAT) & 0xFF
            retries = ocd.mrw(MB_RETRIES)

            if nread == 0:
                print(f"[{addr:#010x}] 0 bytes — NAND end or unreadable")
                break

            tmp = os.path.join(
                tempfile.gettempdir(),
                f"nand_chunk_{os.getpid()}.bin",
            )
            ocd.dump_image(tmp, BUF_ADDR, nread)
            actual_size = os.path.getsize(tmp)
            if actual_size != nread:
                raise OpenOCDError(
                    f"dump_image size mismatch: expected {nread}, got {actual_size}"
                )

            with open(tmp, "rb") as f:
                data = f.read()
                outf.write(data)
            os.unlink(tmp)

            addr += nread
            nchunks += 1
            pct = (addr - start) * 100 // total_bytes
            elapsed = time.time() - t_start
            rate = (addr - start) / elapsed / 1024 if elapsed > 0 else 0

            print(f"[{addr:#010x}] {pct:3d}%  {nread:6d} bytes  "
                  f"{rate:6.1f} KiB/s  [{nchunks} chunks]  "
                  f"status=0x{last_stat:02x} retries={retries}",
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
