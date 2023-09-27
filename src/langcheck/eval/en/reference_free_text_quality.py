import json
from typing import Dict, List, Optional

import openai
import torch
from detoxify import Detoxify
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from langcheck._handle_logs import _handle_logging_level
from langcheck.eval.eval_value import EvalValue
from langcheck.stats import compute_stats

_sentiment_model_path = "cardiffnlp/twitter-roberta-base-sentiment-latest"
_sentiment_tokenizer = None
_sentiment_model = None

_fluency_model_path = "prithivida/parrot_fluency_model"
_fluency_tokenizer = None
_fluency_model = None

_toxicity_model = None


def sentiment(generated_outputs: List[str],
              prompts: Optional[List[str]] = None,
              model_type: str = 'local',
              openai_args: Optional[Dict[str, str]] = None) -> EvalValue[float]:
    '''Calculates the sentiment scores of generated outputs using the
    Twitter-roBERTa-base model. This metric takes on float values between
    [0, 1], where 0 is negative sentiment and 1 is positive sentiment.

    Args:
        generated_outputs: A list of model generated outputs to evaluate
        prompts: An optional list of prompts used to generate the outputs.
            Prompts are not evaluated and only used as metadata.

    Returns:
        An :class:`~langcheck.eval.eval_value.EvalValue` object
    '''
    assert model_type in ['local', 'openai'
                         ], ('Unsupported model type. '
                             'The supported ones are ["local", "openai"]')

    if model_type == 'local':
        scores = _sentiment_local(generated_outputs)
    else:  # openai
        scores = _sentiment_openai(generated_outputs, openai_args)

    return EvalValue(metric_name='sentiment',
                     prompts=prompts,
                     generated_outputs=generated_outputs,
                     reference_outputs=None,
                     sources=None,
                     metric_values=scores,
                     language='en')


def _sentiment_local(generated_outputs: List[str]) -> List[float]:
    '''Calculates the sentiment scores of generated outputs using the
    Twitter-roBERTa-base model. This metric takes on float values between
    [0, 1], where 0 is negative sentiment and 1 is positive sentiment.

    Ref:
        https://huggingface.co/cardiffnlp/twitter-roberta-base-sentiment-latest

    Args:
        generated_outputs: A list of model generated outputs to evaluate
        prompts: An optional list of prompts used to generate the outputs.
            Prompts are not evaluated and only used as metadata.

    Returns:
        A list of scores
    '''
    global _sentiment_tokenizer, _sentiment_model

    if _sentiment_tokenizer is None or _sentiment_model is None:
        _sentiment_tokenizer = AutoTokenizer.from_pretrained(
            _sentiment_model_path)

        # There is a "Some weights are not used warning" but we ignore it
        # because that is intended.
        with _handle_logging_level():
            _sentiment_model = (AutoModelForSequenceClassification.
                                from_pretrained(_sentiment_model_path))

    input_tokens = _sentiment_tokenizer(generated_outputs,
                                        return_tensors='pt',
                                        padding=True)

    with torch.no_grad():
        # Probabilities of [negative, neutral, positive]
        probs = torch.nn.functional.softmax(
            _sentiment_model(**input_tokens).logits, dim=1)

    return (probs[:, 1] / 2 + probs[:, 2]).tolist()


def _sentiment_openai(
        generated_outputs: List[str],
        openai_args: Optional[Dict[str, str]] = None) -> List[float]:
    '''Calculates the sentiment scores of generated outputs using the OpenAI
    API. This metric takes on float values that are either 0, 0.5, or 1, where 0
    is negative sentiment, 0.5 is neutral sentiment, and 1 is positive
    sentiment.  We leverage the function calling API to make sure that the
    output is structured such that we can compute a score.

    Ref:
        https://platform.openai.com/docs/guides/gpt/function-calling

    Args:
        generated_outputs: A list of model generated outputs to evaluate
        openai_args: Dict of additional args to pass in to the
            `openai.ChatCompletion.create` function, default None

    Returns:
        A list of scores
    '''

    def _prompt(gen_output: str) -> str:
        return f'''
        You are evaluating the sentiment of a submitted statement. Here is the
        data:
        [BEGIN DATA]
        ************
        [Submission]: {gen_output}
        ************
        [END DATA]

        Determine the predominant sentiment of the submitted statement. The
        available assessments are:
        `Positive` - The submitted statement has a predominantly positive
        sentiment
        `Negative` - The submitted statement has a predominantly negative
        sentiment
        `Neutral` - The submitted statement has neither a positive nor negative
        sentiment
        '''

    def _sentiment_assessment_to_score(assessment: str) -> float:
        if assessment == 'Positive':
            return 1.0
        elif assessment == 'Neutral':
            return 0.5
        elif assessment == 'Negative':
            return 0.0
        else:
            # By leveraging the function calling API, this should be pretty
            # rare, but we're dealing with LLMs here so nothing is absolute!
            raise AssertionError(
                'OpenAI returned an unrecognized sentiment assessment :(')

    score_list = []
    for gen in generated_outputs:
        messages = [{"role": "user", "content": _prompt(gen_output=gen)}]
        functions = [{
            "name": "save_sentiment_assessment",
            "description": "Save's a statement's sentiment assessment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sentiment": {
                        "type":
                            "string",
                        "enum": ["Positive", "Negative", "Neutral"],
                        "description":
                            "The sentiment assessment of the statement",
                    },
                },
                "required": ["sentiment"],
            },
        }]
        if openai_args is None:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=messages,
                functions=functions,
                function_call={"name": "save_sentiment_assessment"},
            )
        else:
            response = openai.ChatCompletion.create(
                messages=messages,
                functions=functions,
                function_call={"name": "save_sentiment_assessment"},
                **openai_args,
            )
        response_message = response["choices"][0]["message"]
        function_args = json.loads(
            response_message["function_call"]["arguments"])
        sentiment_assessment = function_args.get("sentiment")
        score_list.append(_sentiment_assessment_to_score(sentiment_assessment))
    return score_list


def fluency(generated_outputs: List[str],
            prompts: Optional[List[str]] = None) -> EvalValue[float]:
    '''Calculates the fluency scores of generated outputs using the Parrot
    fluency model. This metric takes on float values between [0, 1], where 0 is
    low fluency and 1 is high fluency.

    Ref:
        https://huggingface.co/prithivida/parrot_fluency_model

    Args:
        generated_outputs: A list of model generated outputs to evaluate
        prompts: An optional list of prompts used to generate the outputs.
            Prompts are not evaluated and only used as metadata.

    Returns:
        An :class:`~langcheck.eval.eval_value.EvalValue` object
    '''
    global _fluency_tokenizer, _fluency_model

    if _fluency_tokenizer is None or _fluency_model is None:
        _fluency_tokenizer = AutoTokenizer.from_pretrained(_fluency_model_path)

        # There is a "Some weights are not used warning" but we ignore it
        # because that is intended.
        with _handle_logging_level():
            _fluency_model = AutoModelForSequenceClassification.from_pretrained(
                _fluency_model_path)

    input_tokens = _fluency_tokenizer(generated_outputs,
                                      return_tensors='pt',
                                      padding=True)

    with torch.no_grad():
        # Probabilities of [negative, neutral, positive]
        probs = torch.nn.functional.softmax(
            _fluency_model(**input_tokens).logits, dim=1)

    scores = probs[:, 1].tolist()

    return EvalValue(metric_name='fluency',
                     prompts=prompts,
                     generated_outputs=generated_outputs,
                     reference_outputs=None,
                     sources=None,
                     metric_values=scores,
                     language='en')


def toxicity(generated_outputs: List[str],
             prompts: Optional[List[str]] = None) -> EvalValue[float]:
    '''Calculates the toxicity scores of generated outputs using the Detoxify
    model. This metric takes on float values between [0, 1], where 0 is low
    toxicity and 1 is high toxicity.

    Ref:
        https://github.com/unitaryai/detoxify

    Args:
        generated_outputs: A list of model generated outputs to evaluate
        prompts: An optional list of prompts used to generate the outputs.
            Prompts are not evaluated and only used as metadata.

    Returns:
        An :class:`~langcheck.eval.eval_value.EvalValue` object
    '''
    global _toxicity_model
    if _toxicity_model is None:
        _toxicity_model = Detoxify('original')
    scores = _toxicity_model.predict(generated_outputs)['toxicity']

    return EvalValue(metric_name='toxicity',
                     prompts=prompts,
                     generated_outputs=generated_outputs,
                     reference_outputs=None,
                     sources=None,
                     metric_values=scores,
                     language='en')


def flesch_reading_ease(
        generated_outputs: List[str],
        prompts: Optional[List[str]] = None) -> EvalValue[float]:
    '''Calculates the readability of generated outputs using the Flesch Reading
    Ease Score. This metric takes on float values between (-∞, 121.22], but
    typically ranges between 0 and 100, where higher scores mean the text is
    easier to read.

    The score is based on the number of sentences, words, and syllables in the
    text. See "How to Write Plain English" by Rudolf Franz Flesch for more
    details.

    Args:
        generated_outputs: A list of model generated outputs to evaluate
        prompts: An optional list of prompts used to generate the outputs.
            Prompts are not evaluated and only used as metadata.

    Returns:
        An :class:`~langcheck.eval.eval_value.EvalValue` object
    '''
    output_stats = [compute_stats(output) for output in generated_outputs]
    scores = [
        206.835 - 1.015 * (stat.num_words / stat.num_sentences) - 84.6 *
        (stat.num_syllables / stat.num_words) for stat in output_stats
    ]
    return EvalValue(metric_name='flesch_reading_ease',
                     prompts=prompts,
                     generated_outputs=generated_outputs,
                     reference_outputs=None,
                     sources=None,
                     metric_values=scores,
                     language='en')


def flesch_kincaid_grade(
        generated_outputs: List[str],
        prompts: Optional[List[str]] = None) -> EvalValue[float]:
    '''Calculates the readability of generated outputs using the Flesch-Kincaid
    Grade Level metric. This metric takes on float values between [-3.40, ∞),
    but typically ranges between 0 and 12 (corresponding to U.S. grade levels),
    where lower scores mean the text is easier to read.

    Like the Flesch Reading Ease Score, this metric is based on the number of
    sentences, words, and syllables in the text.

    Ref:
        https://apps.dtic.mil/sti/citations/ADA006655

    Args:
        generated_outputs: A list of model generated outputs to evaluate
        prompts: An optional list of prompts used to generate the outputs.
            Prompts are not evaluated and only used as metadata.

    Returns:
        An :class:`~langcheck.eval.eval_value.EvalValue` object
    '''
    output_stats = [compute_stats(output) for output in generated_outputs]
    scores = [
        0.39 * (stat.num_words / stat.num_sentences) + 11.8 *
        (stat.num_syllables / stat.num_words) - 15.59 for stat in output_stats
    ]
    return EvalValue(metric_name='flesch_kincaid_grade',
                     prompts=prompts,
                     generated_outputs=generated_outputs,
                     reference_outputs=None,
                     sources=None,
                     metric_values=scores,
                     language='en')
