#!/usr/bin/env bash
# Generates documentation for FiftyOne.
#
# Copyright 2017-2020, Voxel51, Inc.
# voxel51.com
#


# Show usage information
usage() {
    echo "Usage:  bash $0 [-h] [-c] [-s]

Options:
-h      Display this help message.
-c      Perform a clean build (deletes existing build directory).
-s      Copy static files only (CSS, JS)
"
}


# Parse flags
SHOW_HELP=false
CLEAN_BUILD=false
STATIC_ONLY=false
while getopts "hcs" FLAG; do
    case "${FLAG}" in
        h) SHOW_HELP=true ;;
        c) CLEAN_BUILD=true ;;
        s) STATIC_ONLY=true ;;
        *) usage; exit 2 ;;
    esac
done
[ ${SHOW_HELP} = true ] && usage && exit 0


set -e

export FIFTYONE_HEADLESS=1

THIS_DIR=$(dirname "$0")

if [[ ${STATIC_ONLY} = true ]]; then
    echo "**** Updating static files ****"
    rsync -av "${THIS_DIR}/source/_static/" "${THIS_DIR}/build/html/_static/"
    exit 0
fi


if [[ ${CLEAN_BUILD} = true ]]; then
    echo "**** Deleting existing build directories ****"
    rm -rf "${THIS_DIR}/build"
fi


echo "**** Generating documentation ****"

cd ${THIS_DIR}

# Build docs
# sphinx-build [OPTIONS] SOURCEDIR OUTPUTDIR [FILENAMES...]
sphinx-build -M html source build $SPHINXOPTS

echo "**** Documentation complete ****"
printf "To view the docs, open:\n\ndocs/build/html/index.html\n\n"
