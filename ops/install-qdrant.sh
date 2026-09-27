#!/usr/bin/env bash
# Native Qdrant install (spec p31). Verifies published archive SHA-256 before extraction.
# Usage: bash ops/install-qdrant.sh   (run from project root after Python/uv setup)
set -eu
mkdir -p tools/qdrant .downloads ops .data/qdrant .data/sources
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64)
    ASSET=qdrant-x86_64-unknown-linux-gnu.tar.gz
    SHA=eef986e769d4d3e806dd2d546e1b4ecdd416211e54d34b4ed764fac7c58e1085
    ;;
  Linux/aarch64|Linux/arm64)
    ASSET=qdrant-aarch64-unknown-linux-musl.tar.gz
    SHA=0e607c11705fab22f7d667f4749bc0b6b60a8fa9e91de71880a6ebafbbda1b26
    ;;
  Darwin/arm64)
    ASSET=qdrant-aarch64-apple-darwin.tar.gz
    SHA=e060209dfefc9d977ddcec48521349f505f8fd1ce21f2a3db444140870522fe4
    ;;
  Darwin/x86_64)
    ASSET=qdrant-x86_64-apple-darwin.tar.gz
    SHA=ba7cbada9a90aefdbd7f92de4e093cd25328206bd65e9e96657bd6133d272637
    ;;
  *) printf '%s\n' "Unsupported platform for this script"; exit 1 ;;
esac
BASE=https://github.com/qdrant/qdrant/releases/download/v1.19.1
curl --fail --location --silent --show-error "$BASE/$ASSET" -o .downloads/qdrant.tar.gz
python -c 'import hashlib,pathlib,sys; h=hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest(); sys.exit(h != sys.argv[2])' .downloads/qdrant.tar.gz "$SHA"
tar -xzf .downloads/qdrant.tar.gz -C tools/qdrant
chmod +x tools/qdrant/qdrant
./tools/qdrant/qdrant --version
echo "Pinned release v1.19.1 verified. Config: ops/qdrant.native.yaml"
