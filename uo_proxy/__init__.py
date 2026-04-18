"""UO MITM proxy — capture demo trajectories from ClassicUO without modifying it.

Sits between ClassicUO and the real UO server, forwards bytes unchanged, and
logs decoded packets to JSONL. Reuses the Anima codec (anima.client.codec,
anima.client.packets) so server-side Huffman decompression and packet framing
behave identically to the agent.
"""
