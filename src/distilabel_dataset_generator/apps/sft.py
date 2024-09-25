import io
import multiprocessing
import time
from typing import Union

import gradio as gr
import pandas as pd
from datasets import Dataset
from distilabel.distiset import Distiset
from gradio.oauth import OAuthToken
from huggingface_hub import upload_file

from src.distilabel_dataset_generator.pipelines.sft import (
    DEFAULT_DATASET_DESCRIPTIONS,
    DEFAULT_DATASETS,
    DEFAULT_SYSTEM_PROMPTS,
    PROMPT_CREATION_PROMPT,
    generate_pipeline_code,
    get_pipeline,
    get_prompt_generation_step,
)
from src.distilabel_dataset_generator.utils import (
    get_login_button,
    get_org_dropdown,
    swap_visibilty,
)


def _run_pipeline(result_queue, num_turns, num_rows, system_prompt, is_sample):
    pipeline = get_pipeline(num_turns, num_rows, system_prompt, is_sample)
    distiset: Distiset = pipeline.run(use_cache=False)
    result_queue.put(distiset)


def generate_system_prompt(dataset_description, progress=gr.Progress()):
    if dataset_description in DEFAULT_DATASET_DESCRIPTIONS:
        index = DEFAULT_DATASET_DESCRIPTIONS.index(dataset_description)
        if index < len(DEFAULT_SYSTEM_PROMPTS):
            return DEFAULT_SYSTEM_PROMPTS[index]

    progress(0.1, desc="Initializing text generation")
    generate_description = get_prompt_generation_step()
    progress(0.4, desc="Loading model")
    generate_description.load()
    progress(0.7, desc="Generating system prompt")
    result = next(
        generate_description.process(
            [
                {
                    "system_prompt": PROMPT_CREATION_PROMPT,
                    "instruction": dataset_description,
                }
            ]
        )
    )[0]["generation"]
    progress(1.0, desc="System prompt generated")
    return result


def generate_sample_dataset(system_prompt, progress=gr.Progress()):
    if system_prompt in DEFAULT_SYSTEM_PROMPTS:
        index = DEFAULT_SYSTEM_PROMPTS.index(system_prompt)
        if index < len(DEFAULT_DATASETS):
            return DEFAULT_DATASETS[index]

    progress(0.1, desc="Initializing sample dataset generation")
    result = generate_dataset(
        system_prompt, num_turns=1, num_rows=1, progress=progress, is_sample=True
    )
    progress(1.0, desc="Sample dataset generated")
    return result


def _check_push_to_hub(org_name, repo_name):
    repo_id = (
        f"{org_name}/{repo_name}"
        if repo_name is not None and org_name is not None
        else None
    )
    if repo_id is not None:
        if not all([repo_id, org_name, repo_name]):
            raise gr.Error(
                "Please provide a `repo_name` and `org_name` to push the dataset to."
            )
    return repo_id


def generate_dataset(
    system_prompt: str,
    num_turns: int = 1,
    num_rows: int = 5,
    is_sample: bool = False,
    progress=gr.Progress(),
):
    if num_rows < 5:
        duration = 25
    elif num_rows < 10:
        duration = 60
    elif num_rows < 30:
        duration = 120
    elif num_rows < 100:
        duration = 240
    elif num_rows < 300:
        duration = 600
    elif num_rows < 1000:
        duration = 1200
    else:
        duration = 2400

    result_queue = multiprocessing.Queue()
    p = multiprocessing.Process(
        target=_run_pipeline,
        args=(result_queue, num_turns, num_rows, system_prompt, is_sample),
    )

    try:
        p.start()
        total_steps = 100
        for step in range(total_steps):
            if not p.is_alive() or p._popen.poll() is not None:
                break
            progress(
                (step + 1) / total_steps,
                desc=f"Generating dataset with {num_rows} rows. Don't close this window.",
            )
            time.sleep(duration / total_steps)  # Adjust this value based on your needs
        p.join()
    except Exception as e:
        raise gr.Error(f"An error occurred during dataset generation: {str(e)}")

    distiset = result_queue.get()

    # If not pushing to hub generate the dataset directly
    distiset = distiset["default"]["train"]
    if num_turns == 1:
        outputs = distiset.to_pandas()[["prompt", "completion"]]
    else:
        outputs = distiset.to_pandas()[["messages"]]
    dataframe = pd.DataFrame(outputs)

    progress(1.0, desc="Dataset generation completed")
    return dataframe


def push_to_hub(
    dataframe,
    private: bool = True,
    org_name: str = None,
    repo_name: str = None,
    oauth_token: Union[OAuthToken, None] = None,
):
    repo_id = _check_push_to_hub(org_name, repo_name)
    distiset = Distiset(
        {
            "default": Dataset.from_pandas(dataframe),
        }
    )
    distiset.push_to_hub(
        repo_id=repo_id,
        private=private,
        include_script=True,
        token=oauth_token,
    )
    return dataframe


def upload_pipeline_code(
    pipeline_code, org_name, repo_name, oauth_token: Union[OAuthToken, None] = None
):
    with io.BytesIO(pipeline_code.encode("utf-8")) as f:
        upload_file(
            path_or_fileobj=f,
            path_in_repo="pipeline.py",
            repo_id=f"{org_name}/{repo_name}",
            repo_type="dataset",
            token=oauth_token,
            commit_message="Include pipeline script",
        )


css = """
.main_ui_logged_out{opacity: 0.3; pointer-events: none}
"""

with gr.Blocks(
    title="🧬 Synthetic Data Generator",
    head="🧬  Synthetic Data Generator",
    css=css,
) as app:
    with gr.Row():
        gr.Markdown(
            "Want to run this locally or with other LLMs? Take a look at the FAQ tab. distilabel Synthetic Data Generator is free, we use the authentication token to push the dataset to the Hugging Face Hub and not for data generation."
        )
    with gr.Row():
        gr.Column()
        get_login_button()
        gr.Column()

    gr.Markdown("## Iterate on a sample dataset")
    with gr.Column() as main_ui:
        dataset_description = gr.TextArea(
            label="Give a precise description of the assistant or tool. Don't describe the dataset",
            value=DEFAULT_DATASET_DESCRIPTIONS[0],
            lines=2,
        )
        examples = gr.Examples(
            elem_id="system_prompt_examples",
            examples=[[example] for example in DEFAULT_DATASET_DESCRIPTIONS],
            inputs=[dataset_description],
        )
        with gr.Row():
            gr.Column(scale=1)
            btn_generate_system_prompt = gr.Button(value="Generate sample")
            gr.Column(scale=1)

        system_prompt = gr.TextArea(
            label="System prompt for dataset generation. You can tune it and regenerate the sample",
            value=DEFAULT_SYSTEM_PROMPTS[0],
            lines=5,
        )

        with gr.Row():
            sample_dataset = gr.DataFrame(
                value=DEFAULT_DATASETS[0],
                label="Sample dataset. Prompts and completions truncated to 256 tokens.",
                interactive=False,
                wrap=True,
            )

        with gr.Row():
            gr.Column(scale=1)
            btn_generate_sample_dataset = gr.Button(
                value="Regenerate sample",
            )
            gr.Column(scale=1)

        result = btn_generate_system_prompt.click(
            fn=generate_system_prompt,
            inputs=[dataset_description],
            outputs=[system_prompt],
            show_progress=True,
        ).then(
            fn=generate_sample_dataset,
            inputs=[system_prompt],
            outputs=[sample_dataset],
            show_progress=True,
        )

        btn_generate_sample_dataset.click(
            fn=generate_sample_dataset,
            inputs=[system_prompt],
            outputs=[sample_dataset],
            show_progress=True,
        )

        # Add a header for the full dataset generation section
        gr.Markdown("## Generate full dataset")
        gr.Markdown(
            "Once you're satisfied with the sample, generate a larger dataset and push it to the Hub."
        )

        with gr.Column() as push_to_hub_ui:
            with gr.Row(variant="panel"):
                num_turns = gr.Number(
                    value=1,
                    label="Number of turns in the conversation",
                    minimum=1,
                    maximum=4,
                    step=1,
                    info="Choose between 1 (single turn with 'instruction-response' columns) and 2-4 (multi-turn conversation with a 'messages' column).",
                )
                num_rows = gr.Number(
                    value=10,
                    label="Number of rows in the dataset",
                    minimum=1,
                    maximum=500,
                    info="The number of rows in the dataset. Note that you are able to generate more rows at once but that this will take time.",
                )
            with gr.Row(variant="panel"):
                org_name = get_org_dropdown()
                repo_name = gr.Textbox(
                    label="Repo name", placeholder="dataset_name", value="my-distiset"
                )
                private = gr.Checkbox(
                    label="Private dataset",
                    value=True,
                    interactive=True,
                    scale=0.5,
                )
            with gr.Row() as regenerate_row:
                btn_generate_full_dataset = gr.Button(
                    value="Generate", variant="primary", scale=2
                )
                btn_generate_and_push_to_hub = gr.Button(
                    value="Generate and Push to Hub", variant="primary", scale=2
                )
                btn_push_to_hub = gr.Button(
                    value="Push to Hub", variant="primary", scale=2
                )
            with gr.Row():
                final_dataset = gr.DataFrame(
                    value=DEFAULT_DATASETS[0],
                    label="Generated dataset",
                    interactive=False,
                    wrap=True,
                )

            with gr.Row():
                success_message = gr.Markdown(visible=False)

    def show_success_message(org_name, repo_name):
        return gr.Markdown(
            value=f"""
            <div style="padding: 1em; background-color: #e6f3e6; border-radius: 5px; margin-top: 1em;">
                <h3 style="color: #2e7d32; margin: 0;">Dataset Published Successfully!</h3>
                <p style="margin-top: 0.5em;">
                    The generated dataset is in the right format for fine-tuning with TRL, AutoTrain or other frameworks.
                    Your dataset is now available at:
                    <a href="https://huggingface.co/datasets/{org_name}/{repo_name}" target="_blank" style="color: #1565c0; text-decoration: none;">
                        https://huggingface.co/datasets/{org_name}/{repo_name}
                    </a>
                </p>
            </div>
        """,
            visible=True,
        )

    def hide_success_message():
        return gr.Markdown(visible=False)

    gr.Markdown("## Or run this pipeline locally with distilabel")

    with gr.Accordion(
        "Run this pipeline using distilabel",
        open=False,
    ):
        pipeline_code = gr.Code(
            value=generate_pipeline_code(
                system_prompt.value, num_turns.value, num_rows.value
            ),
            language="python",
            label="Distilabel Pipeline Code",
        )

    sample_dataset.change(
        fn=lambda x: x,
        inputs=[sample_dataset],
        outputs=[final_dataset],
    )

    btn_generate_full_dataset.click(
        fn=hide_success_message,
        outputs=[success_message],
    ).then(
        fn=generate_dataset,
        inputs=[system_prompt, num_turns, num_rows],
        outputs=[final_dataset],
        show_progress=True,
    )
    btn_generate_and_push_to_hub.click(
        fn=hide_success_message,
        outputs=[success_message],
    ).then(
        fn=generate_dataset,
        inputs=[system_prompt, num_turns, num_rows],
        outputs=[final_dataset],
        show_progress=True,
    ).then(
        fn=push_to_hub,
        inputs=[final_dataset, private, org_name, repo_name],
        outputs=[final_dataset],
        show_progress=True,
    ).then(
        fn=upload_pipeline_code,
        inputs=[pipeline_code, org_name, repo_name],
        outputs=[],
    ).success(
        fn=show_success_message,
        inputs=[org_name, repo_name],
        outputs=[success_message],
    )

    btn_push_to_hub.click(
        fn=push_to_hub,
        inputs=[final_dataset, private, org_name, repo_name],
        outputs=[final_dataset],
    ).then(
        fn=upload_pipeline_code,
        inputs=[pipeline_code, org_name, repo_name],
        outputs=[],
    ).success(
        fn=show_success_message,
        inputs=[org_name, repo_name],
        outputs=[success_message],
    )

    system_prompt.change(
        fn=generate_pipeline_code,
        inputs=[system_prompt, num_turns, num_rows],
        outputs=[pipeline_code],
    )
    num_turns.change(
        fn=generate_pipeline_code,
        inputs=[system_prompt, num_turns, num_rows],
        outputs=[pipeline_code],
    )
    num_rows.change(
        fn=generate_pipeline_code,
        inputs=[system_prompt, num_turns, num_rows],
        outputs=[pipeline_code],
    )
    app.load(get_org_dropdown, outputs=[org_name])
    app.load(fn=swap_visibilty, outputs=main_ui)
