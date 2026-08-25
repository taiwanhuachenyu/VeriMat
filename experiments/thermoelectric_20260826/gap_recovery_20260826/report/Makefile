SOURCE_DATE_EPOCH := 1755000000
export SOURCE_DATE_EPOCH
export FORCE_SOURCE_DATE := 1
survey.pdf: survey.tex references.bib
	pdflatex -interaction=nonstopmode -halt-on-error survey
	bibtex survey
	pdflatex -interaction=nonstopmode -halt-on-error survey
	pdflatex -interaction=nonstopmode -halt-on-error survey
clean:
	rm -f survey.aux survey.bbl survey.blg survey.log survey.out survey.toc survey.pdf
.PHONY: clean
