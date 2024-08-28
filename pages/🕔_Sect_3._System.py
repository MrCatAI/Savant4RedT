import io
import copy
import warnings
import os
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional
import streamlit as st
import torch
from torch import nn
import transformers
from transformers.generation.utils import (LogitsProcessorList,
                                           StoppingCriteriaList)
from transformers.utils import logging
from ipex_llm.transformers import AutoModelForCausalLM
from transformers import AutoTokenizer  # isort: skip
from openai import OpenAI
from PIL import Image

# 添加自定义 CSS 样式
st.markdown(
    """
    <style>
    .stChatInput {
        background-color: #e6e6fa; /* 浅淡紫色 */
    }
    .stDownloadButton {
        background-color: #f5f5f5;
        color: black;
        border: 0px solid #000;
        border-radius: 5px;
        text-align: center;
        padding: 10px 20px;
        font-size: 16px;
        font-weight: bold;
        cursor: pointer;
        transition: background-color 1.2s;
        display: block;
        margin: 20px auto;
        text-align: center;
    }
    .stDownloadButton:hover {
        background-color: #ff9999;
    }
    </style>
    """,
    unsafe_allow_html=True
)

MODEL_PATH = "/home/merged/internlm2-chat-1_8b"
USER_AVATAR = 'resource/pic_user.png'
ROBOT_AVATAR = 'resource/pic_bot.png'
logger = logging.get_logger(__name__)


@dataclass
class GenerationConfig:
    # this config is used for chat to provide more diversity
    max_length: int = 8192
    top_p: float = 0.6
    temperature: float = 0.6
    do_sample: bool = True
    repetition_penalty: float = 1.00


@torch.inference_mode()
def generate_interactive(
        model,
        tokenizer,
        prompt,
        generation_config: Optional[GenerationConfig] = None,
        logits_processor: Optional[LogitsProcessorList] = None,
        stopping_criteria: Optional[StoppingCriteriaList] = None,
        prefix_allowed_tokens_fn: Optional[Callable[[int, torch.Tensor],
                                                    List[int]]] = None,
        additional_eos_token_id: Optional[int] = None,
        **kwargs,
):
    inputs = tokenizer([prompt], padding=True, return_tensors='pt')
    input_length = len(inputs['input_ids'][0])
    input_ids = inputs['input_ids']
    _, input_ids_seq_length = input_ids.shape[0], input_ids.shape[-1]
    if generation_config is None:
        generation_config = model.generation_config
    generation_config = copy.deepcopy(generation_config)
    model_kwargs = generation_config.update(**kwargs)
    bos_token_id, eos_token_id = (  # noqa: F841  # pylint: disable=W0612
        generation_config.bos_token_id,
        generation_config.eos_token_id,
    )
    if isinstance(eos_token_id, int):
        eos_token_id = [eos_token_id]
    if additional_eos_token_id is not None:
        eos_token_id.append(additional_eos_token_id)
    has_default_max_length = kwargs.get(
        'max_length') is None and generation_config.max_length is not None
    if has_default_max_length and generation_config.max_new_tokens is None:
        warnings.warn(
            f"Using 'max_length''s default \
                ({repr(generation_config.max_length)}) \
                to control the generation length. "
            'This behaviour is deprecated and will be removed from the \
                config in v5 of Transformers -- we'
            ' recommend using `max_new_tokens` to control the maximum \
                length of the generation.',
            UserWarning,
        )
    elif generation_config.max_new_tokens is not None:
        generation_config.max_length = generation_config.max_new_tokens + \
                                       input_ids_seq_length
        if not has_default_max_length:
            logger.warn(  # pylint: disable=W4902
                f"Both 'max_new_tokens' (={generation_config.max_new_tokens}) "
                f"and 'max_length'(={generation_config.max_length}) seem to "
                "have been set. 'max_new_tokens' will take precedence. "
                'Please refer to the documentation for more information. '
                '(https://huggingface.co/docs/transformers/main/'
                'en/main_classes/text_generation)',
                UserWarning,
            )

    if input_ids_seq_length >= generation_config.max_length:
        input_ids_string = 'input_ids'
        logger.warning(
            f'Input length of {input_ids_string} is {input_ids_seq_length}, '
            f"but 'max_length' is set to {generation_config.max_length}. "
            'This can lead to unexpected behavior. You should consider'
            " increasing 'max_new_tokens'.")

    # 2. Set generation parameters if not already defined
    logits_processor = logits_processor if logits_processor is not None \
        else LogitsProcessorList()
    stopping_criteria = stopping_criteria if stopping_criteria is not None \
        else StoppingCriteriaList()

    logits_processor = model._get_logits_processor(
        generation_config=generation_config,
        input_ids_seq_length=input_ids_seq_length,
        encoder_input_ids=input_ids,
        prefix_allowed_tokens_fn=prefix_allowed_tokens_fn,
        logits_processor=logits_processor,
    )

    stopping_criteria = model._get_stopping_criteria(
        generation_config=generation_config,
        stopping_criteria=stopping_criteria)

    logits_warper = model._get_logits_warper(generation_config)

    unfinished_sequences = input_ids.new(input_ids.shape[0]).fill_(1)
    scores = None
    while True:
        model_inputs = model.prepare_inputs_for_generation(
            input_ids, **model_kwargs)
        # forward pass to get next token
        outputs = model(
            **model_inputs,
            return_dict=True,
            output_attentions=False,
            output_hidden_states=False,
        )

        next_token_logits = outputs.logits[:, -1, :]

        # pre-process distribution
        next_token_scores = logits_processor(input_ids, next_token_logits)
        next_token_scores = logits_warper(input_ids, next_token_scores)

        # sample
        probs = nn.functional.softmax(next_token_scores, dim=-1)
        if generation_config.do_sample:
            next_tokens = torch.multinomial(probs, num_samples=1).squeeze(1)
        else:
            next_tokens = torch.argmax(probs, dim=-1)

        # update generated ids, model inputs, and length for next step
        input_ids = torch.cat([input_ids, next_tokens[:, None]], dim=-1)
        model_kwargs = model._update_model_kwargs_for_generation(
            outputs, model_kwargs, is_encoder_decoder=False)
        unfinished_sequences = unfinished_sequences.mul(
            (min(next_tokens != i for i in eos_token_id)).long())

        output_token_ids = input_ids[0].cpu().tolist()
        output_token_ids = output_token_ids[input_length:]
        for each_eos_token_id in eos_token_id:
            if output_token_ids[-1] == each_eos_token_id:
                output_token_ids = output_token_ids[:-1]
        response = tokenizer.decode(output_token_ids)
        # fix format
        response = response.replace('## 结论总结\n', '## 结论总结\n\n')
        response = response.replace('\n---', '\n\n---')
        yield response
        # stop when each sentence is finished
        # or if we exceed the maximum length
        if unfinished_sequences.max() == 0 or stopping_criteria(
                input_ids, scores):
            break


@torch.inference_mode()
def generate(
        model,
        tokenizer,
        prompt,
        generation_config: Optional[GenerationConfig] = None,
        logits_processor: Optional[LogitsProcessorList] = None,
        stopping_criteria: Optional[StoppingCriteriaList] = None,
        prefix_allowed_tokens_fn: Optional[Callable[[int, torch.Tensor],
                                                    List[int]]] = None,
        additional_eos_token_id: Optional[int] = None,
        **kwargs,
):
    for cur_response in generate_interactive(
            model=model,
            tokenizer=tokenizer,
            prompt=prompt,
            additional_eos_token_id=additional_eos_token_id,
            **asdict(generation_config),
    ):
        pass
    return cur_response


def chat_api(client, model, instruction):
    st.session_state.history.append({'role': 'user', 'content': instruction})
    chat = client.chat.completions.create(
        model=model,
        messages=st.session_state.history,
    )
    return chat.choices[0].message.content


def on_btn_click():
    del st.session_state.sec3_messages


@st.cache_resource
def load_model():
    model = (AutoModelForCausalLM.from_pretrained(
        MODEL_PATH,
        trust_remote_code=True, load_in_4bit=True))
    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH,
                                              trust_remote_code=True)
    return model, tokenizer


@st.cache_resource
def load_api():
    client = OpenAI(
        api_key=os.getenv('API_KEY', ''),
        base_url=os.getenv('API_BASE_URL', '')
    )
    model = os.getenv('API_MODEL', '')
    return client, model


def prepare_generation_config():
    with st.sidebar:
        max_length = st.slider('Max Length',
                               min_value=8,
                               max_value=16384,
                               value=8192)
        top_p = st.slider('Top P', 0.0, 1.0, 0.6, step=0.01)
        temperature = st.slider('Temperature', 0.0, 1.0, 0.6, step=0.01)
        st.button('Clear Chat History', on_click=on_btn_click)

    generation_config = GenerationConfig(max_length=max_length,
                                         top_p=top_p,
                                         temperature=temperature)

    return generation_config


user_prompt = '<|im_start|>user\n{user}<|im_end|>\n'
robot_prompt = '<|im_start|>assistant\n{robot}<|im_end|>\n'
cur_query_prompt = '<|im_start|>user\n{user}<|im_end|>\n\
    <|im_start|>assistant\n'


def combine_history(prompt):
    messages = st.session_state.sec3_messages
    meta_instruction = "你是 AI 信息内容安全专家"
    total_prompt = f'<s><|im_start|>system\n{meta_instruction}<|im_end|>\n'
    for message in messages:
        cur_content = message['content']
        if message['role'] == 'user':
            cur_prompt = user_prompt.format(user=cur_content)
        elif message['role'] == 'robot':
            cur_prompt = robot_prompt.format(robot=cur_content)
        else:
            pass
        total_prompt += cur_prompt
    total_prompt = total_prompt + cur_query_prompt.format(user=prompt)
    return total_prompt


def extract(message):
    if '安全等级划分' not in message:
        return '检测失败！'
    index = message.rfind('安全等级划分')
    for i in range(index, len(message)):
        if message[i] == '低':
            return '低'
        elif message[i] == '中':
            return '中'
        elif message[i] == '高':
            return '高'
        else:
            continue


def generate_markdown():
    messages = st.session_state.sec3_messages
    markdown_content = ""
    for message in messages:
        role = message['role']
        content = message['content']
        if role == 'user':
            markdown_content += f"**User:** {content}\n\n"
        elif role == 'robot':
            markdown_content += f"**Robot:** {content}\n\n"
    return markdown_content


def main():
    print('load model begin.')
    model, tokenizer = load_model()
    print('load model end.')
    print('load client begin')
    client, api_model = load_api()
    print('load client end')

    st.title('🛡️ Savant4RedT && 系统模型')
    image_logo = Image.open('resource/pic_usage_3.png')
    st.sidebar.image(
        image_logo,
        use_column_width=True
    )
    st.sidebar.markdown('## 🍏 Model Configuration')

    generation_config = prepare_generation_config()

    # Initialize chat history
    if 'sec3_messages' not in st.session_state:
        st.session_state.sec3_messages = []
        st.session_state.history = []
    
    # Display chat messages from history on app rerun
    for message in st.session_state.sec3_messages:
        if message['role'] == 'user':
            with st.chat_message('user', avatar=message['avatar']):
                st.markdown(message['content'])
        if message['role'] == 'api':
            with st.chat_message('assistant'):
                st.markdown(message['content'])
        if message['role'] == 'robot':
            with st.chat_message('robot', avatar=message['avatar']):
                st.markdown(message['content'])
    
    # Accept user input
    if prompt := st.chat_input('本生成式模型接口由 InternLM-API 进行推理，输入输出受到 Savant4RedT-1.8B-Content 进行检测'):
        with st.chat_message('user', avatar=USER_AVATAR):
            st.markdown(prompt)
        st.session_state.sec3_messages.append({
            'role': 'user',
            'content': prompt,
            'avatar': USER_AVATAR
        })
        # detect
        detect_prompt = "**请你分析以下内容：**\n```\n" + prompt + "\n```\n**输出分析结果**"
        detect_prompt = combine_history(detect_prompt)
        detect_result = generate(model=model,
                tokenizer=tokenizer,
                prompt=detect_prompt,
                additional_eos_token_id=92542,
                generation_config=generation_config)
        detect_extract = extract(detect_result)
        if detect_extract != '低':
            with st.chat_message('robot', avatar=ROBOT_AVATAR):
                st.markdown(detect_result)
            st.session_state.sec3_messages.append({
                'role': 'robot',
                'content': detect_result,  # pylint: disable=undefined-loop-variable
                'avatar': ROBOT_AVATAR,
            })
        # whether api
        else:
            result = chat_api(client, api_model, prompt)
            detect_prompt = "**请你分析以下内容：**\n```\n" + result + "\n```\n**输出分析结果**"
            detect_prompt = combine_history(detect_prompt)
            detect_result = generate(model=model,
                    tokenizer=tokenizer,
                    prompt=detect_prompt,
                    additional_eos_token_id=92542,
                    generation_config=generation_config)
            if extract(detect_result) == '低':
                with st.chat_message('assistant'):
                    st.markdown(result)
                st.session_state.sec3_messages.append({
                    'role': 'api',
                    'content': result,  # pylint: disable=undefined-loop-variable
                })
            else:
                with st.chat_message('robot', avatar=ROBOT_AVATAR):
                    st.markdown(detect_result)
                st.session_state.sec3_messages.append({
                    'role': 'robot',
                    'content': detect_result,  # pylint: disable=undefined-loop-variable
                    'avatar': ROBOT_AVATAR,
                })


if __name__ == '__main__':
    main()
