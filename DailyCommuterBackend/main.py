from fastapi import APIRouter, Request, Depends, Form, HTTPException, status
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import RedirectResponse
from dotenv import load_dotenv
import os

from fastapi import FastAPI
from path.to.your.module import router as home_router

app = FastAPI()
app.include_router(home_router, prefix="/home")

# change whenever we change the name of the app
from DailyCommuterBackend.auth import login_required
from DailyCommuterBackend.db import get_db
from DailyCommuterBackend.apiRouting.api import createRoute, Router

router = APIRouter()
templates = Jinja2Templates(directory="templates")

load_dotenv()
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN")
TRANSIT_TOKEN = os.getenv("TRANSIT_TOKEN")

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    db = get_db()
    trip_update = db.execute(
        'SELECT * FROM trip_update WHERE route_id = "GS"'
    ).fetchall()

    for alert in trip_update:
        print(dict(alert))

    return templates.TemplateResponse("home/index.html", {
        "request": request,
        "trip_update": trip_update
    })


@router.get("/displayroute/{routeid}", response_class=HTMLResponse)
async def map_view(request: Request, routeid: str):
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT name, lat, lon, type
        FROM points
        WHERE routeid = ?
        ORDER BY type ASC
    ''', (routeid,))
    rows = c.fetchall()
    conn.close()

    stops = [
        {'name': name, 'lat': lat, 'lon': lon, 'type': type}
        for name, lat, lon, type in rows
    ]
    return templates.TemplateResponse("home/map.html", {
        "request": request,
        "stops": stops,
        "MAPBOX_TOKEN": MAPBOX_TOKEN
    })


@router.post("/addRoute")
async def create_route_post(request: Request):
    try:
        data = await request.json()
        start_address = data['start_address']
        end_address = data['end_address']
        arriveby = data['arriveby']
        userid = '69'  # placeholder

        print("before route created")
        print(start_address, end_address, arriveby, userid)

        newroute = createRoute(start_address, end_address, arriveby, userid)
        print("route created")
        Router(newroute)

        # Equivalent to Flask's url_for
        redirect_url = request.url_for("map_view", routeid=newroute.id)
        return JSONResponse(content={"redirect_url": str(redirect_url)})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error: {e}")

