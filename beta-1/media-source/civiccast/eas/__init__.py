# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Public-safety alert ingest + on-channel display (S11c).

CivicCast ingests CAP/IPAWS, NWS, and state AMBER alerts and DISPLAYS them as
on-channel information (crawl / overlay / operator-confirmed slate) through the
existing CG/overlay render path. It is **not an EAS device**: it does not relay the
FCC Part 11 EAS signal, generate SAME headers, or perform automatic forced
pre-emption. The mandatory Part 11 relay remains the cable operator's certified
headend equipment. Every artifact in this module stamps ``eas_claim="not_eas"``.
"""
