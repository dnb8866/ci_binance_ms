import asyncio
import datetime
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import binance
import logging

import config
from utils.models import (
    UniqueUserRequest,
    RequestForServer,
    Way,
    ResponseKline,
    Period,
    ResponseGetTicker,
    Price,
    PercentOfPoint,
    PercentOfTime, TypeRequest)
from utils.repositories import Repository


class Monitoring:
    """
    Класс для мониторинга запросов пользователей.
    """

    def __init__(self, client: binance.Client, repo: Repository):
        self.client = client
        self.repo = repo
        self.response_from_server = None
        self.cumulative_weight = 0
        self.price_all_tickers = {}
        self.list_tickers = set()

    async def get_ticker_price(self, ticker: str) -> float:
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            data = await loop.run_in_executor(pool, partial(self.client.get_ticker, symbol=ticker))
            return float(data['lastPrice'])

    async def get_list_tickers(self):
        while True:
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as pool:
                tickers_data = await loop.run_in_executor(pool, self.client.get_all_tickers)
                for ticker_data in tickers_data:
                    if ticker_data['symbol'].endswith('USDT'):
                        self.list_tickers.add(ticker_data['symbol'])
            await asyncio.sleep(86400)

    async def reset_weight(self):
        while True:
            await asyncio.sleep(61)
            print('reset_weight')
            self.cumulative_weight = 0

    async def _price_check_change(self, request: UniqueUserRequest, response: dict) -> UniqueUserRequest | None:
        try:
            klines = response[TypeRequest.price][request.symbol]
            if request.way == Way.up_to:
                max_price = max([kline.high_price for kline in klines])
                if request.request_data.target_price <= max_price:
                    return request
            if request.way == Way.down_to:
                min_price = min([kline.low_price for kline in klines])
                if request.request_data.target_price >= min_price:
                    return request
        except Exception as e:
            logging.exception(f'{self.__class__.__qualname__} - {e}')

    async def _percent_of_point_check_change(self, request: UniqueUserRequest,
                                             response: dict) -> UniqueUserRequest | None:
        try:
            klines = response[TypeRequest.price][request.symbol]
            if request.way in (Way.up_to, Way.all):
                max_price = max([kline.high_price for kline in klines])
                delta = max_price - request.request_data.current_price
                if delta / request.request_data.current_price * 100 >= request.request_data.target_percent:
                    return request
            if request.way in (Way.down_to, Way.all):
                min_price = min([kline.low_price for kline in klines])
                delta = request.request_data.current_price - min_price
                if delta / request.request_data.current_price * 100 >= request.request_data.target_percent:
                    return request
        except Exception as e:
            logging.exception(f'{self.__class__.__qualname__} - {e}')

    async def _percent_of_time_check_change(self, request: UniqueUserRequest,
                                            response: dict) -> UniqueUserRequest | None:
        try:
            ticker = response[TypeRequest.period][request.symbol][request.request_data.period.value]
            if request.way == Way.up_to:
                if ticker.price_change_percent >= request.request_data.target_percent:
                    return request
            elif request.way == Way.down_to:
                if ticker.price_change_percent <= -request.request_data.target_percent:
                    return request
            else:
                if abs(ticker.price_change_percent) >= request.request_data.target_percent:
                    return request
        except Exception as e:
            logging.exception(f'{self.__class__.__qualname__} - {e}')

    async def _get_response_price_or_percent_of_point(self, request: RequestForServer, delay: float) -> None:
        await asyncio.sleep(delay)
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            for _ in range(config.TRY_GET_RESPONSE):
                while self.cumulative_weight > config.CUMULATIVE_WEIGHT_THROTTLING:
                    print('throttling')
                    await asyncio.sleep(1)
                self.cumulative_weight += request.request_data.weight
                try:
                    response = await loop.run_in_executor(pool, partial(self.client.get_klines,
                                                                        symbol=request.symbol,
                                                                        interval=config.INTERVAL_FOR_PRICE_REQUEST,
                                                                        limit=config.LIMIT_FOR_PRICE_REQUEST))
                    list_response = [ResponseKline(*map(float, i[:11])) for i in response]
                    self.response_from_server[TypeRequest.price].update({request.symbol: list_response})
                    break
                except Exception as e:
                    await asyncio.sleep(config.TIMEOUT_BETWEEN_RESPONSE)
                    print(e)
                    continue

    async def _get_response_percent_of_time(self, request: RequestForServer, delay: float) -> None:
        await asyncio.sleep(delay)
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            for _ in range(config.TRY_GET_RESPONSE):
                while self.cumulative_weight > config.CUMULATIVE_WEIGHT_THROTTLING:
                    print('throttling')
                    await asyncio.sleep(1)
                self.cumulative_weight += request.request_data.weight
                try:
                    response = await loop.run_in_executor(pool, partial(self.client.get_ticker,
                                                                        symbol=request.symbol))
                    if request.symbol not in response:
                        self.response_from_server[TypeRequest.period].update({request.symbol: {}})
                    self.response_from_server[TypeRequest.period][request.symbol].update(
                        {request.request_data.period.value: ResponseGetTicker(response)}
                    )
                    break
                except Exception as e:
                    await asyncio.sleep(config.TIMEOUT_BETWEEN_RESPONSE)
                    str(e)
                    continue

    async def _check_change(
            self,
            request: UniqueUserRequest,
            response_from_server: dict
    ):
        """
        Проверяет один запрос на изменение.

        Args:
            request: Запрос
            response_from_server: Ответ сервера в Dict

        Returns: Возвращает запрос, если он достиг или превысил значения.
        """
        if isinstance(request.request_data, Price):
            return await self._price_check_change(request, response_from_server)
        if isinstance(request.request_data, PercentOfPoint):
            return await self._percent_of_point_check_change(request, response_from_server)
        if isinstance(request.request_data, PercentOfTime):
            return await self._percent_of_time_check_change(request, response_from_server)

    async def get_response_from_server(self, requests_for_server: list[RequestForServer]) -> dict:
        """
        Получает ответы от сервера по множеству запросов в многопоточном режиме.
        Args:
            requests_for_server: Перечень уникальных запросов на сервер в виде множества set.
        Returns:
            {
            <TypeRequest.period>:
                    {'symbol': {TypeRequest.period.value: ResponseGetTicker},
                     'symbol': {TypeRequest.period.value: ResponseGetTicker}}
            <TypeRequest.price>:
                    {'symbol': [ResponseKline[...]],
                     'symbol': [ResponseKline[...]]}
            }
        """

        tasks = []
        delay = 0

        self.response_from_server = {TypeRequest.price: {}, TypeRequest.period: {}}

        async def reqs_for_server(reqs):
            for req in reqs:
                await asyncio.sleep(0)
                yield req

        async for request in reqs_for_server(requests_for_server):
            if isinstance(request.request_data, (Price, PercentOfPoint)):
                tasks.append(asyncio.create_task(self._get_response_price_or_percent_of_point(request, delay)))
                delay += config.INTERVAL_BETWEEN_RESPONSE
            if isinstance(request.request_data, PercentOfTime) and request.request_data.period == Period.v_24h:
                tasks.append(asyncio.create_task(self._get_response_percent_of_time(request, delay)))
                delay += config.INTERVAL_BETWEEN_RESPONSE

        for task in tasks:
            await task

        return self.response_from_server

    async def check_all_changes(self):
        """
        Запускает мониторинг запросов
        """

        while True:
            res = []

            await self.repo.load_requests_from_remote_repo()
            user_requests = await self.repo.get_unique_user_requests()
            unique_requests_for_server = await self.repo.get_unique_requests_for_server()
            response_from_server = await self.get_response_from_server(unique_requests_for_server)

            print(user_requests)
            print(unique_requests_for_server)
            print(response_from_server)

            for request in user_requests:
                if await self._check_change(request, response_from_server):
                    res.append(request)

            print(res)
            await asyncio.sleep(1)
            # TODO: отправка в RabbitMQ
