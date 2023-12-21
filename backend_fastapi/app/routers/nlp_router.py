import copy
from typing import List, Union

from asyncer import asyncify
from fastapi import APIRouter
from fastapi import Response, status
from pydantic import BaseModel, ValidationError

from app.data.data import prompt_data, OpenaiModel, lock, Extraction, PromptIdentifier, MedicalDataPrompt, Symptom, \
    SymptomICD10, ExtractionICD10
from app.utils.embedding_util import EmbeddingUtil
from app.utils.general_util import parse_json_from_string
from app.utils.openai_util import (
    OpenAIUtil,
    OpenaiCompletionConfig,
    OpenaiCompletionBody,
)


class AnalyzeBody(BaseModel):
    """Body for an /analyze request"""

    text: str


class EmbeddingBody(BaseModel):
    """Body for an /embedding request"""

    text: str
    amount: int = 10


class EmbeddingEndpointOutput(BaseModel):
    code: str
    text: str


llmRouter = APIRouter(
    prefix="/api/nlp",
    tags=["nlp"],
)

# init dependencies
openaiUtil = OpenAIUtil()
embedUtil = EmbeddingUtil()


@llmRouter.post("/hello/{model}")
async def hello(model: OpenaiModel, config: OpenaiCompletionConfig):
    """Hello World example for large language models"""
    openaiUtil.openai_model = model
    return await openaiUtil.hello_chat_completion(config, model)


@llmRouter.post("/openai/{model}")
async def openai(model: OpenaiModel, body: OpenaiCompletionBody):
    """Non-Streaming OpenAI chat completion"""
    return await openaiUtil.chat_completion(body.messages, body.config, model)


@llmRouter.post("/embedding")
async def getEmbedding(body: EmbeddingBody) -> List[EmbeddingEndpointOutput]:
    # pandas dataframe is not thread-safe. Therefore, we need to use a lock
    async with lock:
        res = await asyncify(embedUtil.search)(
            embedUtil.icd10_symptoms, body.text, body.amount
        )
        output = res[["schlüsselnummer_mit_punkt", "klassentitel"]].rename(
            columns={"schlüsselnummer_mit_punkt": "code", "klassentitel": "text"}
        )
    return output.to_dict(orient="records")


# TODO: add logic for using embeddings or not using then
# TODO: add logic for summary of patient history/symptoms as freetext
@llmRouter.post("/analyze")
async def analyze(body: AnalyzeBody, response: Response) -> Union[ExtractionICD10, str]:
    """Endpoint for analyzing a conversation between a doctor and his patient.
    Returns an HTTP-206 code if no valid JSON was parsed"""
    # get the prompt for the analyze request
    prompt = await get_prompt(body.text, PromptIdentifier.SYMPTOM_EXTRACT_JSON)
    if prompt is None:
        response.status_code = status.HTTP_404_NOT_FOUND
        return f"No prompt found for identifier"

    extraction = await get_extraction(prompt)
    # TODO: hier logik einbauen um noch die anamnese zu extrahieren
    if isinstance(extraction, ExtractionICD10):
        return extraction
    if isinstance(extraction, str):
        response.status_code = status.HTTP_206_PARTIAL_CONTENT
        return extraction
    else:
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return "No medical data could be extracted"


async def get_extraction(prompt: MedicalDataPrompt, use_embeddings: bool = True):
    output = await openaiUtil.chat_completion(
        prompt.messages,
        OpenaiCompletionConfig(
            max_tokens=4096, response_format={"type": "json_object"}
        ),
        OpenaiModel.GPT_3_TURBO_1106,
    )
    if use_embeddings:
        # parse output and validate
        parsed = parse_json_from_string(output)
        try:
            validated = Extraction.model_validate(parsed)
            # add icd10 codes to extraction
            extraction_with_codes = ExtractionICD10(symptoms=await get_icd10_symptoms(validated.symptoms))
            return extraction_with_codes
        except ValidationError:
            return output
    else:
        # TODO: hier einbauen, dass auch direkt icd10 annotierte daten returniertwerden ohne embeddings nur mit prompt
        raise NotImplementedError


async def get_icd10_symptoms(symptoms: List[Symptom]) -> List[SymptomICD10]:
    output = []
    async with lock:
        for symptom in symptoms:
            res = await asyncify(embedUtil.search)(
                embedUtil.icd10_symptoms, symptom.context, 1
            )
            icd10_string = (
                    res["schlüsselnummer_mit_punkt"].iloc[0]
                    + " - "
                    + res["klassentitel"].iloc[0]
            )
            output.append(SymptomICD10(
                icd10=icd10_string,
                context=symptom.context,
                location=symptom.location,
                symptom=symptom.symptom,
                onset=symptom.onset,
            ))
    return output


async def get_prompt(text: str, identifier: PromptIdentifier) -> MedicalDataPrompt | None:
    """Returns a prompt for the given identifier. The placeholder for the userinput will be replaced with the text"""
    async with lock:
        prompt_data_copy = copy.deepcopy(prompt_data)

    found_prompts = []
    for pr in prompt_data_copy.prompts:
        if pr.identifier == identifier:
            found_prompts.append(pr)
    if len(found_prompts) == 0:
        return None

    prompt = found_prompts[0]
    # replace the placeholder from the prompt with the message from the user
    for message in prompt.messages:
        if prompt_data_copy.userinput_placeholder in message.content:
            message.content = message.content.replace(
                prompt_data_copy.userinput_placeholder, text
            )
    return prompt
