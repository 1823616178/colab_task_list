[Bug] AI quota blocked: "User location HK is not supported" but I'm in Germany

1. Summary of problem

Every Kaggle Benchmarks model call on my account (laycatbot) fails with:

    openai.PermissionDeniedError: User location "HK" is not supported for this model/API

All models affected (Claude, Gemini, DeepSeek, Qwen...). My real location/IP is Germany, not Hong Kong — the region flag seems wrong.

2. Steps to reproduce

    kaggle b t run codegen-task -m gemini-3-flash-preview
    # or locally: kaggle benchmarks init -y && source .env && python codegen_task.py

3. Expected behavior

Model returns a completion and consumes AI quota. Instead the proxy rejects every request with the HK location error, so the $10/day AI quota is completely unusable.

4. Screenshots

Errored runs: https://www.kaggle.com/benchmarks/tasks/laycatbot/codegen-task/1
