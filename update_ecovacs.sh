#!/bin/bash

echo "Fetching upstream..."
git fetch upstream

echo "Pulling Ecovacs integration..."
git read-tree --prefix=custom_components/ecovacs/ -u upstream/dev:homeassistant/components/ecovacs

echo "Bumping version..."
VERSION=$(grep '"version"' custom_components/ecovacs/manifest.json | sed -E 's/.*"([0-9]+\.[0-9]+\.)([0-9]+)".*/\2/')
NEW_VERSION=$((VERSION+1))

sed -i -E "s/\"version\": \"[0-9]+\.[0-9]+\.[0-9]+\"/\"version\": \"1.0.$NEW_VERSION\"/" custom_components/ecovacs/manifest.json

echo "Committing..."
git add .
git commit -m "Sync Ecovacs with upstream (v1.0.$NEW_VERSION)"
git push

echo "Done!"