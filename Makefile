# Synthetic sample end to end: PDF with OCG layers -> floor files -> layer SVG -> unit polygons ->
# render check -> simulated manual edit in Figma -> diff by id.
SAMPLE := FLOORPLAN_CONFIG=config/sample.json
RUN := uv run
PORT ?= 8765

.PHONY: install sample test editor clean

install:
	uv sync
	$(RUN) playwright install chromium

sample:
	$(SAMPLE) $(RUN) sample/make_sample_pdf.py
	$(SAMPLE) $(RUN) tools/floor-index.py
	$(SAMPLE) $(RUN) tools/plan-pdf-layers.py 2 3 --inventory
	$(SAMPLE) $(RUN) tools/plan-units-from-pdf.py --all
	$(SAMPLE) $(RUN) tools/render-check.py 2 3 --units
	$(SAMPLE) $(RUN) sample/fake_figma_edit.py 2
	$(SAMPLE) $(RUN) tools/figma-floor-export.py 2 sample/work/tmp/figma-export-2.svg
	node tools/plan-build.mjs --plans editor/plans --out sample/work/plan-out --labels

test:
	$(RUN) pytest -q

editor:
	@echo "plan editor: http://127.0.0.1:$(PORT)/editor/app/   style studio: http://127.0.0.1:$(PORT)/studio/"
	$(RUN) python -m http.server $(PORT) --bind 127.0.0.1

clean:
	rm -rf sample/work
