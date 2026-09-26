python_version_supported() {
  "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'
}
