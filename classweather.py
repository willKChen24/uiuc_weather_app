import asyncio
import aiohttp
import os
import zoneinfo  #for timezones
from datetime import datetime, timedelta, time, timezone

from aiohttp.web import RouteTableDef, run_app, Application, Request, Response, FileResponse, json_response
routes = RouteTableDef()

#uiuc timezone
uiuc_tz = zoneinfo.ZoneInfo('America/Chicago')

#global cache for weather data
WEATHER_CACHE = {}

@routes.get('/')
async def index(request: Request) -> FileResponse:  # done for you
    return FileResponse("index.html")


@routes.post('/weather')
async def POST_weather(request: Request) -> Response:
    try:
        data = await request.json()
        course = data.get("course", "").strip()
        
        #normalize course format (CS 340, cs340, etc.)
        #extract letters and numbers
        letters = ""
        numbers = ""
        for char in course:
            if char.isalpha():
                letters += char
            elif char.isdigit():
                numbers += char
        
        #normalize format to uppercase letters and 3 digits
        letters = letters.upper()
        if not letters or len(numbers) != 3: #error handling
            return json_response({"error": "Invalid course format"}, status=400)
        
        formatted_course = f"{letters} {numbers}"
        
        #check if we have cached data for this course
        if formatted_course in WEATHER_CACHE:
            return json_response(WEATHER_CACHE[formatted_course])
            
        #get the microservice URL from environment variable
        server_url = os.getenv('COURSES_MICROSERVICE_URL')
        if not server_url: #more error handling
            return json_response({"error": "Course microservice URL not configured"}, status=500)
    
        course_path = f"/{letters}/{numbers}/" #define course path
        url = server_url + course_path #append server URL and course path to get the official URL
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return json_response({"error": "Course not found"}, status=400)
                
                course_data = await response.json()
                
                #convert the format from what the microservice returns to what get_next_meeting_time expects
                converted_data = convert_course_data_format(course_data)
                
                #calculate next meeting time
                next_meeting = get_next_meeting_time(converted_data)
                if not next_meeting:
                    return json_response({"error": "No upcoming meetings found for this course"}, status=400)
                
                #format time for response
                meeting_time_str = next_meeting.strftime("%Y-%m-%d %H:%M:%S")
                
                #get forecast time (rounded down to hour)
                forecast_time = next_meeting.replace(minute=0, second=0, microsecond=0)
                forecast_time_str = forecast_time.strftime("%Y-%m-%d %H:%M:%S")
                
                #get weather forecast from NWS API
                weather_data = await fetch_weather_forecast(forecast_time)
                
                #build response
                response_data = {
                    "course": formatted_course,
                    "nextCourseMeeting": meeting_time_str,
                    "forecastTime": forecast_time_str,
                    "temperature": weather_data["temperature"],
                    "shortForecast": weather_data["shortForecast"]
                }
                
                #store the response data as the value for the formatted course key
                #in the global cache
                WEATHER_CACHE[formatted_course] = response_data
                
                return json_response(response_data)
    
    except Exception as e:
        print(f"Error in POST_weather: {e}")
        return json_response({"error": str(e)}, status=400)


@routes.get('/weatherCache')
async def get_cached_weather(request: Request) -> Response:
    #return the current state of the cache
    return json_response(WEATHER_CACHE)


def convert_course_data_format(course_data):
    #microservice error handling
    if "error" in course_data:
        return {"meeting_times": []}
    
    #extract days of week and convert to full names
    days_of_week = course_data.get("Days of Week", "")
    day_mapping = {
        "M": "Monday",
        "T": "Tuesday",
        "W": "Wednesday",
        "R": "Thursday",
        "F": "Friday",
        "S": "Saturday",
        "U": "Sunday"
    }
    
    days_list = [day_mapping.get(day, "") for day in days_of_week if day in day_mapping]

    #filter out any empty strings
    days_list = [day for day in days_list if day]
    
    #extract and format start time (convert from "11:00 AM" to "11:00")
    start_time = course_data.get("Start Time", "")
    formatted_time = start_time
    
    #if in 12-hour format (with AM/PM), convert to 24-hour format
    if " AM" in start_time or " PM" in start_time:
        try:
            dt = datetime.strptime(start_time, "%I:%M %p")
            formatted_time = dt.strftime("%H:%M")
        except ValueError:
            #if we can't parse the times, keep the original format
            pass
    
    #create the meeting_times structure
    converted_data = {
        "meeting_times": [
            {
                "days": days_list,
                "start_time": formatted_time
            }
        ] if days_list and formatted_time else []
    }
    
    return converted_data


def get_next_meeting_time(course_data):
    now = datetime.now(uiuc_tz)
    meeting_times = course_data.get("meeting_times", [])
    
    if not meeting_times:
        return None
    
    #list to store potential next meetings
    next_meetings = []
    
    for meeting in meeting_times:
        days = meeting.get("days", [])
        start_time = meeting.get("start_time")
        
        if not days or not start_time:
            continue
        
        #parse start time (assuming format like "14:00")
        hour, minute = map(int, start_time.split(':'))
        
        #check each day of the week
        for day in days:
            #convert day name to weekday number (0-6, where 0 is Monday)
            weekday_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}
            if day not in weekday_map:
                continue
                
            weekday = weekday_map[day]
            
            #calculate days until next occurrence of this weekday
            days_ahead = (weekday - now.weekday()) % 7
            
            #if it's the same day, check if the time has passed
            if days_ahead == 0:
                meeting_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if meeting_time <= now:
                    #if meeting time has passed, go to next week
                    days_ahead = 7
            
            #calculate the next meeting datetime
            next_meeting = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
            next_meetings.append(next_meeting)
    
    #find the earliest next meeting
    if next_meetings:
        return min(next_meetings)
    return None


async def fetch_weather_forecast(forecast_time):
    try:
        #UIUC coordinates (40.11,-88.24)
        lat, lon = 40.11, -88.24
        
        #call the API
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.weather.gov/points/{lat},{lon}") as response:
                if response.status != 200:
                    print(f"Error getting points: {response.status}")
                    return {"temperature": "forecast unavailable", "shortForecast": "forecast unavailable"}
                
                points_data = await response.json()
                forecast_hourly_url = points_data["properties"]["forecastHourly"]
                
                #get hourly forecast
                async with session.get(forecast_hourly_url) as forecast_response:
                    if forecast_response.status != 200:
                        print(f"Error getting forecast: {forecast_response.status}")
                        return {"temperature": "forecast unavailable", "shortForecast": "forecast unavailable"}
                    
                    forecast_data = await forecast_response.json()
                    periods = forecast_data["properties"]["periods"]
                    
                    #convert forecast_time to UTC for comparison with API data
                    target_time_utc = forecast_time.astimezone(timezone.utc)
                    
                    #find the closest forecast period
                    closest_period = None
                    smallest_time_diff = timedelta.max
                    
                    for period in periods:
                        try:
                            period_time = datetime.fromisoformat(period["startTime"].replace('Z', '+00:00'))
                            time_diff = abs(period_time - target_time_utc)
                            
                            if time_diff < smallest_time_diff:
                                smallest_time_diff = time_diff
                                closest_period = period
                        except Exception as e:
                            print(f"Error parsing period time: {e}")
                            continue
                    
                    #use the closest forecast we found
                    if closest_period:
                        return {
                            "temperature": closest_period["temperature"],
                            "shortForecast": closest_period["shortForecast"]
                        }
        
        #if we get here, no forecast was found
        return {"temperature": "forecast unavailable", "shortForecast": "forecast unavailable"}
        
    except Exception as e:
        print(f"Error fetching weather data: {e}")
        return {"temperature": "forecast unavailable", "shortForecast": "forecast unavailable"}

if __name__ == '__main__':  # done for you: run the app with custom host and port
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=str, default="0.0.0.0")
    parser.add_argument('-p', '--port', type=int, default=5000)
    args = parser.parse_args()
    
    app = Application()
    app.add_routes(routes)
    run_app(app, host=args.host, port=args.port)
