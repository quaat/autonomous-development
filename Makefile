.PHONY: check test doctor clean

check: test
	python3 scripts/validate_project.py

test:
	python3 -m unittest discover -s tests -v

doctor:
	python3 scripts/controller.py doctor

clean:
	rm -rf __pycache__ scripts/__pycache__ tests/__pycache__
