# validator

ComputeHorde Validator. See repositories main README for explanation of what that means.

- - -

Skeleton of this project was generated with `cookiecutter-rt-django`, which sometimes gets upgrades that are easy to retrofit into already older projects.

# Base requirements

- docker
- docker-compose
- python 3.11
- [uv](https://docs.astral.sh/uv/)
- [nox](https://nox.thea.codes)

# Setup development environment

```sh
# 1st tab
$ ./setup-dev.sh
```

```sh
# 2nd tab
docker-compose up
```

```sh
# 1st tab
cd app/src
uv run manage.py wait_for_database --timeout 10
uv run manage.py migrate
```

Typically, when developing validator, you want to test:

1. Scheduling synthetic jobs
2. Settings weights
3. Passing organic jobs from a facilitator to a miner

Some of these actions may require launching celery locally, which you can achieve by running [start_celery.sh](dev_env_setup%2Fstart_celery.sh).

Some tests start celery in order to test obscure interprocess timeouts. That is generally automated, however celery 
workers being killed on MacOS yield an insufferable amount of error dialogs displayed on top of everything you have 
open. To save developers from suffering this fate, tests support running celery on a remote host, via ssh. A common case
would be a local VM, also running postgres and redis. In such a case, make sure postgres and redis ports are forwarded
so they are accessible from the host, which runs your code/tests. To make tests start celery on the remote host, define
the following env vars (the author of this note suggests using direnv or PyCharm Run/Debug configuration templates for
ease):

```shell
REMOTE_HOST=ubuntu-dev  # ssh config entry name
REMOTE_VENV=/path/to/remote/venv  # on the remote
REMOTE_CELERY_START_SCRIPT=/project/mount/ComputeHorde/validator/dev_env_setup/start_celery.sh  #  adjust the beginning
# to match your synced folders configuration
```

## Scheduling synthetic jobs

```shell
DEBUG_MINER_KEY=... python manage.py debug_run_synthetic_jobs
```

# Setting up a trusted miner for cross-validation

## Set up a server

Create an Ubuntu server and use the `install_miner.sh` script from the root of this repository to install the miner in a **local mode**:

```sh
curl -sSfL https://github.com/backend-developers-ltd/ComputeHorde/raw/master/install_miner.sh | bash -s - local SSH_DESTINATION VALIDATOR_PUBLIC_KEY MINER_PORT DEFAULT_EXECUTOR_CLASS
```

Replace the placeholders in the command above:
- `SSH_DESTINATION`: your server's connection info (i.e. `username@1.2.3.4`)
- `VALIDATOR_PUBLIC_KEY`: the hotkey of your validator (_not_ the path to the key)
- `MINER_PORT` (optional): the port (of your choosing) on which the miner will listen for incoming connections (default is 8000)
- `DEFAULT_EXECUTOR_CLASS` (optional): specifies a custom executor class to use. **For A6000 synthetic job support, `always_on.llm.a6000` must be set**

## Provision S3 buckets for prompts and answers

Trusted miners require S3 buckets to store prompts and answers. 

**Make sure** you have [AWS CLI](https://aws.amazon.com/cli/) installed and configured, 
to provision these buckets conveniently with the correct permissions pre-configured. 

Then run the following script:

```sh
curl -sSfL https://github.com/backend-developers-ltd/ComputeHorde/raw/master/validator/provision_s3.sh | bash -s - PROMPTS_BUCKET ANSWERS_BUCKET --create-user
```

Replace `PROMPTS_BUCKET` and `ANSWERS_BUCKET` with the names of the S3 buckets you want to use for prompts and answers respectively.

It will automatically create a dedicated user, assign permissions policy for created buckets, and add an access key, 
displaying it at the end so it can be copied to the validator `.env` file. 

If you don't want to create a user and prefer to handle permissions manually, skip the `--create-user` option.

Note: if your buckets are not in your default AWS region export `AWS_DEFAULT_REGION` before running the script (both buckets need to be in the same region), and add it to `.env` later:
```
export AWS_DEFAULT_REGION=BUCKETS_REGION
```

At the end of the script, it will show the values for `S3_BUCKET_NAME_PROMPTS`, `S3_BUCKET_NAME_ANSWERS`.
If you use the `--create-user` flag, it will also show the values for `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
Copy these variables in your validator `.env` file and restart your validator.

> [!WARNING]  
> Even if you did not use `--create-user`, you still need to provide `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in your validator `.env` file.
> In that case, you will have to manually generate the credentials.

> [!NOTE]
> We have tested the AWS S3. The buckets allow quick and concurrent upload and download of multiple (but tiny) text files.

## Verifying S3 bucket configurations

After setting up your `.env` file by following the above section,
you can verify your S3 setup with the following command:

```sh
docker compose exec validator-runner docker compose exec app python manage.py check_s3_setup
```

If your some reason your S3 setup does not work,
you can try checking different values of for S3 configurations with this command's flags:

```
--aws-access-key-id AWS_ACCESS_KEY_ID
                      Override the value of AWS_ACCESS_KEY_ID from .env
--aws-secret-access-key AWS_SECRET_ACCESS_KEY
                      Override the value of AWS_SECRET_ACCESS_KEY from .env
--aws-region-name AWS_REGION_NAME
                      Override the value of AWS region (by default read from environment variable AWS_DEFAULT_REGION)
--aws-endpoint-url AWS_ENDPOINT_URL
                      Override the value of AWS_ENDPOINT_URL from .env
--s3-bucket-name-prompts S3_BUCKET_NAME_PROMPTS
                      Override the value of S3_BUCKET_NAME_PROMPTS from .env
--s3-bucket-name-answers S3_BUCKET_NAME_ANSWERS
                      Override the value of S3_BUCKET_NAME_ANSWERS from .env
--aws-signature-version AWS_SIGNATURE_VERSION
                      Override the value of AWS_SIGNATURE_VERSION from .env
```

After finding the configuration values for which the script works,
copy the values in your validator `.env` file and restart your validator. 

## Updated validator .env

Add or update these variables in the validator `.env` file:

```
TRUSTED_MINER_ADDRESS="MINER_IP"
TRUSTED_MINER_PORT=MINER_PORT
```

If your buckets are not in the default AWS region, add also:

```
AWS_DEFAULT_REGION=BUCKETS_REGION
```


# Using the Collateral Smart Contract (Optional)

Validators can optionally deploy a **collateral smart contract** to filter miners based on **trust expressed through deposited funds**.

Once deployed:

- Your validator will **automatically begin using the contract** to prefer miners who have deposited collateral.
- This enables a **slashing mechanism** for incorrect organic job results, increasing the reliability of ComputeHorde’s compute pool.

To set it up, follow the 
[deployment instructions in the Collateral Contract repository](https://github.com/bactensor/collateral-contracts#recommended-validator-integration-guide-as-used-by-computehorde). 
Once deployed, no additional configuration is needed — the validator detects and uses the contract automatically.

> **Note:** This feature is optional but recommended if your validator sends organic jobs to untrusted miners.

