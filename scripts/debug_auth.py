import asyncio
import httpx

async def test():
    async with httpx.AsyncClient(base_url="http://localhost:8000") as c:
        r = await c.post(
            "/api/v1/auth/register",
            json={"email": "test99@test.com", "password": "Test123456!"},
        )
        print(f"Status: {r.status_code}")
        print(f"Body: {r.text}")

asyncio.run(test())
