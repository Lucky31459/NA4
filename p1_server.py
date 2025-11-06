#!/usr/bin/env python3
"""
Usage:
    python3 p1_server.py <SERVER_IP> <SERVER_PORT> <SWS>

SWS is sender window size in bytes (fixed, no congestion control in Part 1).
Sends data.txt in the current directory to a client that sends a one-byte request.
"""

import socket, sys, os, threading, time, struct, math

MAX_UDP_PAYLOAD = 1200
HEADER_SIZE = 20  # 4 bytes seq + 16 bytes reserved
DATA_SIZE = MAX_UDP_PAYLOAD - HEADER_SIZE  # 1180

# RTT estimation params (RFC-style simplified)
ALPHA = 0.125
BETA = 0.25
INITIAL_RTO = 0.5  # seconds

def make_packet(seq_num: int, data: bytes) -> bytes:
    # seq_num: unsigned 32-bit (byte offset)
    # reserved 16 bytes left zero
    return struct.pack('!I16s', seq_num, b'\x00'*16) + data

def parse_ack(pkt: bytes):
    # ack packet uses same header: seq is next expected byte
    if len(pkt) < 4:
        return None
    seq = struct.unpack('!I', pkt[:4])[0]
    return seq

class Server:
    def __init__(self, ip, port, sws):
        self.addr = (ip, int(port))
        self.SWS = int(sws)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(self.addr)
        self.sock.settimeout(1.0)
        print(f"[Server] Listening on {self.addr}, SWS={self.SWS} bytes")

        self.client_addr = None

        # load file
        if not os.path.exists('data.txt'):
            print("[Server] data.txt not found in current directory.")
            sys.exit(1)
        with open('data.txt', 'rb') as f:
            self.file_bytes = f.read()
        self.file_len = len(self.file_bytes)

        # segmentation
        self.segments = {}  # seq_num -> bytes
        seq = 0
        i = 0
        while seq < self.file_len:
            chunk = self.file_bytes[seq: seq + DATA_SIZE]
            self.segments[seq] = chunk
            seq += len(chunk)
            i += 1
        # EOF will be sent as a special payload 'EOF' at seq = file_len
        self.segments[self.file_len] = b'EOF'

        self.base = 0  # lowest unacked byte (seq number)
        self.next_seq = 0  # next seq to send
        self.timer_lock = threading.Lock()
        self.unacked = {}  # seq -> (packet_bytes, send_time, retransmit_count)
        self.dup_acks = {}  # ack_seq -> count of duplicates
        self.rtt = None
        self.rto = INITIAL_RTO

        self.stop_sending = False

    def start(self):
        print("[Server] Waiting for client request...")
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            # Expect a one-byte request
            if not data:
                continue
            # Accept first request; simple retry up to 5 times done client-side
            self.client_addr = addr
            print(f"[Server] Received request from {addr}, starting transfer.")
            break

        # start ACK listener in thread
        ack_thread = threading.Thread(target=self.ack_listener, daemon=True)
        ack_thread.start()

        self.send_loop()

        # Wait a bit so last ACKs can arrive
        time.sleep(0.5)
        print("[Server] Transfer finished. Closing.")
        self.sock.close()

    def send_loop(self):
        # sliding-window send
        total_bytes = self.file_len + len(b'EOF')  # conceptual
        last_seq = self.file_len  # EOF at this seq

        while not self.stop_sending:
            # send as much as allowed by SWS
            with self.timer_lock:
                in_flight = sum(len(p[0]) - HEADER_SIZE for p in self.unacked.values()) if self.unacked else 0
                # allowed bytes to send
                allowed = self.SWS - in_flight
                # next_seq is next segment seq to attempt sending
                # choose segments in ascending seq order
                seg_seqs = sorted(k for k in self.segments.keys() if k >= self.next_seq)
                for seq in seg_seqs:
                    if seq != self.next_seq:
                        # ensure contiguous advancement
                        break
                    data = self.segments[seq]
                    payload_len = len(data)
                    if payload_len > allowed:
                        break
                    # create packet and send
                    pkt = make_packet(seq, data)
                    try:
                        self.sock.sendto(pkt, self.client_addr)
                    except Exception as e:
                        print("[Server] send error:", e)
                    send_time = time.time()
                    self.unacked[seq] = (pkt, send_time, 0)
                    # advance next_seq to seq + payload_len
                    self.next_seq = seq + payload_len
                    allowed -= payload_len

            # check timeouts / retransmit
            self.check_timeouts()

            # termination condition: if base > last_seq and all ACKed
            with self.timer_lock:
                if self.base > last_seq:
                    # everything beyond EOF acked
                    self.stop_sending = True
                    break

            time.sleep(0.01)

    def check_timeouts(self):
        now = time.time()
        retransmit_list = []
        with self.timer_lock:
            for seq, (pkt, send_time, rcount) in list(self.unacked.items()):
                if now - send_time >= self.rto:
                    retransmit_list.append(seq)
        for seq in retransmit_list:
            with self.timer_lock:
                pkt, old_send, rcount = self.unacked.get(seq, (None, None, None))
                if pkt is None:
                    continue
                # retransmit
                try:
                    self.sock.sendto(pkt, self.client_addr)
                except Exception as e:
                    print("[Server] retransmit send error:", e)
                self.unacked[seq] = (pkt, time.time(), rcount + 1)
                print(f"[Server] Timeout retransmit seq={seq}, rcount={rcount+1}, RTO={self.rto:.3f}")

    def ack_listener(self):
        # listen for ACKs from client
        while True:
            try:
                pkt, addr = self.sock.recvfrom(4096)
            except Exception:
                continue
            if not pkt or len(pkt) < 4:
                continue
            ack_seq = parse_ack(pkt)
            if ack_seq is None:
                continue
            # Only accept from known client
            if self.client_addr and addr != self.client_addr:
                continue

            # update base and RTT
            with self.timer_lock:
                # If ack_seq > base, we can mark all segments with seq < ack_seq as acked
                if ack_seq > self.base:
                    # compute RTT sample from the earliest acked packet (if present)
                    # find largest seq < ack_seq that we had sent
                    acked_seqs = [s for s in self.unacked.keys() if s < ack_seq]
                    if acked_seqs:
                        earliest_seq = min(acked_seqs)
                        pktbytes, send_time, _ = self.unacked[earliest_seq]
                        sample_rtt = time.time() - send_time
                        # update RTT estimate
                        if self.rtt is None:
                            self.rtt = sample_rtt
                            self.rto = max(INITIAL_RTO, self.rtt * 2)
                        else:
                            self.rtt = (1 - ALPHA) * self.rtt + ALPHA * sample_rtt
                            self.rto = max(0.1, (1 - BETA) * self.rto + BETA * abs(sample_rtt - self.rtt))
                    # remove acked segments
                    for s in sorted(list(self.unacked.keys())):
                        if s < ack_seq:
                            del self.unacked[s]
                    self.base = ack_seq
                    # reset dup ack counter for this ack_seq
                    self.dup_acks.pop(ack_seq, None)
                else:
                    # duplicate ack
                    self.dup_acks[ack_seq] = self.dup_acks.get(ack_seq, 0) + 1
                    cnt = self.dup_acks[ack_seq]
                    print(f"[Server] Duplicate ACK for {ack_seq} (count={cnt})")
                    if cnt >= 3:
                        # fast retransmit: retransmit the next outstanding segment (base)
                        if self.base in self.unacked:
                            pkt, _, rcount = self.unacked[self.base]
                            try:
                                self.sock.sendto(pkt, self.client_addr)
                            except Exception:
                                pass
                            self.unacked[self.base] = (pkt, time.time(), rcount + 1)
                            print(f"[Server] Fast retransmit seq={self.base}")
                        # reset dup ack count to avoid repeated retriggers
                        self.dup_acks[ack_seq] = 0

            # if ack for EOF (ack_seq > file_len), check termination
            if ack_seq > self.file_len:
                # client has received EOF and acked it
                with self.timer_lock:
                    self.base = ack_seq
                # break if all sent and acked
                with self.timer_lock:
                    if not self.unacked and self.base > self.file_len:
                        self.stop_sending = True
                        break

if __name__ == '__main__':
    if len(sys.argv) != 4:
        print("Usage: python3 p1_server.py <SERVER_IP> <SERVER_PORT> <SWS>")
        sys.exit(1)
    server = Server(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
    server.start()
