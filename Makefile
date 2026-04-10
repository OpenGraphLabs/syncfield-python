.PHONY: replay-web-install replay-web-build replay-web-dev

replay-web-install:
	cd src/syncfield/replay/_web && yarn install

replay-web-build:
	cd src/syncfield/replay/_web && yarn build

replay-web-dev:
	cd src/syncfield/replay/_web && yarn dev
