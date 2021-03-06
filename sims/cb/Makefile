SHELL:=/bin/bash
.PHONY: getData test run clean retrieve retrieveAll metrics

# EXPERIMENT NAME
EXP_NAME = sim1-noisyclass-fulldata

# RESULTS DIRECTORY
RESULTS_DIR = results/$(EXP_NAME)

# AWS BUCKET
AWS_BUCKET = s3://cytof-vb/cb/$(subst _,-,$(EXP_NAME))

# TEST DIRECTORY
TEST_DIR = $(RESULTS_DIR)/test/

# OLD EXPERIMENTS
#{{{1
# EXP_NAME = sim1-vae
# EXP_NAME = sim1
# EXP_NAME = sim1-subsample
#}}}1

getData:
	aws s3 sync s3://cytof-cb-data data/ --exclude "*" --include "*.txt" --include "*.csv"

sendData:
	aws s3 sync data/ s3://cytof-cb-data --exclude "*" --include "*.txt" --include "*.csv"

run:
	bash run.sh $(RESULTS_DIR)/ $(AWS_BUCKET)

test:
	mkdir -p $(TEST_DIR)
	python3 cb.py $(TEST_DIR) 2 > $(TEST_DIR)/log.txt

clean:
	rm -rf $(RESULTS_DIR)/*

retrieve: 
	aws s3 sync $(AWS_BUCKET) $(RESULTS_DIR) --exclude "*/out.p*"

retrieveAll:
	aws s3 sync $(AWS_BUCKET) $(RESULTS_DIR)

metrics:
	python3 parse_metrics.py results/$(EXP_NAME) elbo
	python3 parse_metrics.py results/$(EXP_NAME) ll
	python3 parse_metrics.py results/$(EXP_NAME) logpost
